import { createConsumer, publish, QUEUES } from '@loop/queue';
import { startService, M, type ServiceContext } from '@loop/runtime';
import { atsDomains, rules } from '@loop/rules';
import type { CandidateMessage, RawMessage } from '@loop/domain';
import { classify, type ClassifierContext } from './classify.js';

/**
 * The classifier service.
 *
 * Concurrency 4: it is pure CPU over strings, it holds nothing, and dropping
 * ~95% of an inbox is the only reason the expensive rungs stay cheap.
 */

interface UserContext {
  companyDomains: Set<string>;
  knownThreads: Set<string>;
  knownNewsletters: Set<string>;
  loadedAt: number;
}

const CONTEXT_TTL_MS = 60_000;

async function loadUserContext(ctx: ServiceContext, userId: string): Promise<UserContext> {
  const client = await ctx.pool.connect();
  try {
    await client.query('select set_config($1,$2,true)', ['loop.user_id', userId]);
    const companies = await client.query<{ domain: string }>(
      `select distinct c.domain from companies c
         join applications a on a.company_id = c.id
        where a.user_id = $1 and c.domain is not null`,
      [userId],
    );
    const threads = await client.query<{ evidence_ref: string }>(
      `select distinct payload->>'thread_id' as evidence_ref
         from application_events
        where user_id = $1 and payload ? 'thread_id'`,
      [userId],
    );
    const newsletters = await client.query<{ domain: string }>(
      `select split_part(provider_message_id, '@', 2) as domain
         from seen_messages
        where user_id = $1 and outcome = 'dropped'
        group by 1 having count(*) >= 5`,
      [userId],
    );
    return {
      companyDomains: new Set(companies.rows.map((r) => r.domain.toLowerCase())),
      knownThreads: new Set(threads.rows.map((r) => r.evidence_ref).filter(Boolean)),
      knownNewsletters: new Set(newsletters.rows.map((r) => r.domain).filter(Boolean)),
      loadedAt: Date.now(),
    };
  } finally {
    client.release();
  }
}

await startService({ name: 'classifier', healthPort: 9101 }, async (ctx) => {
  const registry = await rules();
  const domains = atsDomains(registry);
  ctx.log.info({ msg: 'rule registry loaded', count: registry.length });

  const contexts = new Map<string, UserContext>();
  const contextFor = async (userId: string): Promise<UserContext> => {
    const cached = contexts.get(userId);
    if (cached && Date.now() - cached.loadedAt < CONTEXT_TTL_MS) return cached;
    const fresh = await loadUserContext(ctx, userId);
    contexts.set(userId, fresh);
    return fresh;
  };

  const consumer = createConsumer(ctx.pool, {
    queue: QUEUES.raw,
    batch: 20,
    concurrency: 4,
    log: (line) => ctx.log.debug(line),
  });

  void consumer.start<RawMessage>(async (msg, _envelope, sql) => {
    const userCtx = await contextFor(msg.user_id);
    const classifierCtx: ClassifierContext = {
      atsDomains: domains,
      companyDomains: userCtx.companyDomains,
      knownThreads: userCtx.knownThreads,
      knownNewsletters: userCtx.knownNewsletters,
    };

    const result = classify(msg, classifierCtx);

    if (result.outcome === 'drop') {
      // Recorded, not deleted: a drop is auditable and replayable, which is how
      // a false negative gets found at all.
      await sql.query(
        `update seen_messages set processed_at = now(), outcome = 'dropped'
          where mailbox_id = $1 and provider_message_id = $2`,
        [msg.mailbox_id, msg.provider_message_id],
      );
      M.messagesDropped.inc({ reason: 'classifier' });
      ctx.log.info({
        mailbox_id: msg.mailbox_id,
        provider_message_id: msg.provider_message_id,
        outcome: 'dropped',
        score: result.score,
      });
      return;
    }

    const candidate: CandidateMessage = {
      ...msg,
      score: result.score,
      cheap_only: result.outcome === 'cheap_only',
      reasons: result.reasons,
    };
    await publish(sql, QUEUES.candidate, candidate);
    ctx.log.info({
      mailbox_id: msg.mailbox_id,
      provider_message_id: msg.provider_message_id,
      outcome: result.outcome,
      score: result.score,
    });
  });

  return async () => {
    await consumer.stop();
  };
});
