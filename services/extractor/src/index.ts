import { createConsumer, publish, QUEUES } from '@loop/queue';
import { M, startService } from '@loop/runtime';
import { atsDomains, applyRules, rules, vendorForDomain } from '@loop/rules';
import {
  detectLanguage,
  domainOfAddress,
  excerpt,
  matchesDomainSuffix,
  RESOLVER,
  type CandidateMessage,
  type Channel,
  type Intent,
  type Rung,
  type Signal,
} from '@loop/domain';
import { runRung2 } from './rung2.js';
import { runRung3 } from './rung3.js';
import { buildSignal, channelForVendor, stageForIntent } from './signal.js';

/**
 * The extractor.
 *
 * "The rungs are tried in order and the first one that does not abstain wins;
 * a rung MUST abstain rather than guess." Concurrency 2 — a rung-3 call can
 * hang for thirty seconds, and the whole reason this is its own container is
 * that such a hang must not touch the connector's OAuth tokens.
 */

await startService({
  name: 'extractor',
  healthPort: 9102,
  // Rung 3 is awaited from inside the consumer's transaction, so the connection
  // sits idle-in-transaction for however long inference takes. At the default
  // 30s a local model that thinks before it answers got its backend terminated
  // mid-call, and the resulting pool error took this process down with it. The
  // margin covers the queries either side of the model call; the real fix is to
  // move that call out of the transaction, and until then this is derived from
  // the model's own timeout so the two cannot be configured into crossing.
  idleInTransactionTimeoutMs: (config) => config.model.timeoutMs + 30_000,
}, async (ctx) => {
  const registry = await rules();
  const domains = atsDomains(registry);
  const modelEnabled = !!ctx.config.model.baseUrl;
  ctx.log.info({
    msg: modelEnabled
      ? 'rung 3 enabled'
      : 'rung 3 disabled — unknown templates become review items (failure state F4 is the default posture)',
    count: registry.length,
  });

  const consumer = createConsumer(ctx.pool, {
    queue: QUEUES.candidate,
    batch: 4,
    concurrency: 2,
    log: (line) => ctx.log.debug(line),
  });

  void consumer.start<CandidateMessage>(async (msg, _envelope, sql) => {
    const started = Date.now();
    const senderDomain = domainOfAddress(msg.headers.from);
    const vendor = vendorForDomain(registry, senderDomain);

    // Identity first, and separately from intent: "a reply on a known thread
    // inherits the application with no parsing at all".
    const threadRows = await sql.query<{ thread_id: string; application_id: string }>(
      `select distinct payload->>'thread_id' as thread_id, application_id
         from application_events
        where user_id = $1 and payload ? 'thread_id'`,
      [msg.user_id],
    );
    const threadToApplication = new Map(threadRows.rows.map((r) => [r.thread_id, r.application_id]));
    const rung2 = runRung2(msg, { threadToApplication, atsDomains: domains });

    let intent: Intent | null = null;
    let confidence = 0;
    let rung: Rung = 1;
    let company: string | null = null;
    let role: string | null = null;
    let stageHint: string | null = null;
    let deadline: string | null = null;
    let comp: Signal['comp'] = null;
    let decideBy: string | null = null;

    // ── rung 1 · the template registry ─────────────────────────────────────
    const match = applyRules(registry, msg);
    if (match) {
      intent = match.intent;
      confidence = match.confidence;
      company = match.company;
      role = match.role;
      deadline = match.fields.deadline ?? null;
      rung = 1;
      M.extraction.inc({ rung: 1, outcome: 'hit' });
    }

    // ── rung 2 · calendar and thread heuristics ────────────────────────────
    if (!intent && rung2 && rung2.intent !== 'other') {
      intent = rung2.intent;
      confidence = rung2.confidence;
      company = rung2.company;
      stageHint = rung2.stageHint;
      rung = 2;
      M.extraction.inc({ rung: 2, outcome: 'hit' });
    }

    // ── rung 3 · the model ─────────────────────────────────────────────────
    if (!intent && !msg.cheap_only) {
      const result = await runRung3(
        {
          subject: msg.headers.subject,
          from: msg.headers.from,
          receivedAt: msg.received_at,
          text: msg.text,
        },
        ctx.config.model,
      );

      if (result.status === 'ok') {
        M.modelLatency.observe(result.latencyMs / 1000);
        if (result.violations.length) {
          M.denylistViolations.inc({}, result.violations.length);
          ctx.log.warn({
            msg: 'article 9 fields dropped from model output',
            provider_message_id: msg.provider_message_id,
            violations: result.violations.length,
          });
        }
        intent = result.output.intent;
        confidence = result.output.confidence;
        company = result.output.company;
        role = result.output.role;
        stageHint = result.output.stage_hint;
        deadline = result.output.deadline;
        if (result.output.comp) {
          comp = {
            min_minor: result.output.comp.min === null ? undefined : Math.round(result.output.comp.min * 100),
            max_minor: result.output.comp.max === null ? null : Math.round(result.output.comp.max * 100),
            currency: result.output.comp.currency.toUpperCase(),
          };
        }
        rung = 3;
        M.extraction.inc({ rung: 3, outcome: 'hit' });
      } else if (result.status === 'timeout' || result.status === 'unreachable') {
        // Parked, never dropped. The cron drain re-publishes it, and after six
        // attempts it becomes a review item — which is the promise failure
        // state F4 makes to the user.
        M.modelFailures.inc({ kind: result.status });
        await sql.query(
          `update seen_messages set outcome = 'parked', processed_at = now()
            where mailbox_id = $1 and provider_message_id = $2`,
          [msg.mailbox_id, msg.provider_message_id],
        );
        ctx.log.warn({
          mailbox_id: msg.mailbox_id,
          provider_message_id: msg.provider_message_id,
          outcome: 'parked',
          rung: 3,
          reason: result.status,
        });
        return;
      } else {
        M.extraction.inc({ rung: 3, outcome: result.reason });
      }
    }

    // ── rung 4 · ask the human, once ───────────────────────────────────────
    if (!intent || confidence < RESOLVER.REVIEW_BELOW) {
      await sql.query(
        `insert into review_items (user_id, kind, evidence_ref, excerpt)
         values ($1, 'unknown_intent', $2, $3)
         on conflict do nothing`,
        [msg.user_id, msg.provider_message_id, excerpt(`"${msg.text}" — ${msg.headers.from}`)],
      );
      await sql.query(
        `update seen_messages set outcome = 'review', processed_at = now()
          where mailbox_id = $1 and provider_message_id = $2`,
        [msg.mailbox_id, msg.provider_message_id],
      );
      M.extraction.inc({ rung: 4, outcome: 'review' });
      ctx.log.info({
        mailbox_id: msg.mailbox_id,
        provider_message_id: msg.provider_message_id,
        outcome: 'review',
        confidence,
        duration_ms: Date.now() - started,
      });
      return;
    }

    const channel: Channel | null = channelForVendor(vendor, senderDomain);
    const signal = buildSignal({
      msg,
      intent,
      confidence,
      rung,
      company,
      role,
      stageHint: stageHint ?? stageForIntent(intent),
      deadline,
      comp,
      decideBy,
      vendor,
      channel,
      senderDomain,
      language: detectLanguage(msg.text),
      invite: msg.invite,
      applicationHint: rung2?.applicationId ?? null,
    });

    await publish(sql, QUEUES.signal, signal);
    ctx.log.info({
      mailbox_id: msg.mailbox_id,
      provider_message_id: msg.provider_message_id,
      outcome: 'extracted',
      intent,
      rung,
      confidence,
      vendor: vendor ?? undefined,
      duration_ms: Date.now() - started,
    });
  });

  return async () => {
    await consumer.stop();
  };
});

/** Re-exported so the corpus runner can drive the same code path. */
export { runRung2, runRung3, matchesDomainSuffix };
