import type pg from 'pg';
import {
  matchesDomainSuffix,
  companyKey,
  normaliseCompany,
  normaliseRole,
  RESOLVER,
  type Signal,
} from '@loop/domain';
import { cosine, toVector, type Embedder } from './embed.js';

/**
 * Entity resolution.
 *
 * "The hardest component, and the one whose mistakes are most visible: a wrong
 * merge silently rewrites history. It runs at concurrency 1 per user so two
 * signals cannot create the same application twice."
 *
 * The order below is Engineering Spec §09's pseudocode, line for line.
 */

export type Decision =
  | { kind: 'thread'; applicationId: string; confidence: number }
  | { kind: 'attached'; applicationId: string; cosine: number }
  | { kind: 'created'; applicationId: string }
  | { kind: 'ambiguous'; candidates: Array<{ id: string; cosine: number }> }
  | { kind: 'merged'; applicationId: string; mergedId: string; cosine: number };

export interface ResolveDeps {
  sql: pg.PoolClient;
  embedder: Embedder;
  atsDomains: readonly string[];
  now: Date;
}

interface CandidateRow {
  id: string;
  role_title: string;
  role_normalised: string | null;
  role_embedding: string | null;
  applied_at: Date | null;
  status: string;
  current_stage: string;
  work_mode: string | null;
  location: string | null;
  manually_created: boolean;
}

function parseVector(raw: string | null): number[] | null {
  if (!raw) return null;
  return raw.replace(/^\[|\]$/g, '').split(',').map(Number);
}

/**
 * Company canonicalisation.
 *
 * Domain first because it is the only key that cannot be spelled two ways; the
 * ATS's own domain is subtracted first, since an ATS is never the employer.
 */
export async function canonicaliseCompany(
  deps: ResolveDeps,
  userId: string,
  signal: Signal,
): Promise<string> {
  const { sql } = deps;
  const senderDomain = signal.sender_domain;
  const isAts = !!senderDomain && deps.atsDomains.some((d) => matchesDomainSuffix(senderDomain, d));
  const companyDomain = isAts ? null : senderDomain;

  if (companyDomain) {
    const byDomain = await sql.query<{ id: string }>(
      `select id from companies where domain = $1`,
      [companyDomain],
    );
    if (byDomain.rows[0]) return byDomain.rows[0].id;
  }

  // The alias is keyed on letters and digits only, so "ION Group" arriving from
  // an ATS display name and "iongroup" derived from the company's own domain
  // land on the same row instead of forking the pipeline in two.
  const alias = signal.company ? companyKey(signal.company) : null;
  if (alias) {
    const byAlias = await sql.query<{ company_id: string }>(
      `select company_id from company_aliases where user_id = $1 and alias = $2`,
      [userId, alias],
    );
    if (byAlias.rows[0]) return byAlias.rows[0].company_id;

    const byName = await sql.query<{ id: string }>(
      `select id from companies
        where regexp_replace(lower(canonical_name), '[^a-z0-9]+', '', 'g') = $1
        limit 1`,
      [alias],
    );
    if (byName.rows[0]) {
      await sql.query(
        `insert into company_aliases (user_id, company_id, alias) values ($1,$2,$3)
         on conflict do nothing`,
        [userId, byName.rows[0].id, alias],
      );
      return byName.rows[0].id;
    }
  }

  // A company created from a bare domain still gets a readable name, and the
  // alias below is keyed the same way, so the next spelling finds it.
  const name = signal.company?.trim() || domainLabel(companyDomain) || 'Unknown';
  const created = await sql.query<{ id: string }>(
    `insert into companies (canonical_name, domain) values ($1, $2)
     on conflict (lower(canonical_name), coalesce(domain, '')) do update
       set canonical_name = excluded.canonical_name
     returning id`,
    [name, companyDomain],
  );
  const companyId = created.rows[0]!.id;
  for (const key of new Set([alias, companyKey(name)].filter(Boolean) as string[])) {
    await sql.query(
      `insert into company_aliases (user_id, company_id, alias) values ($1,$2,$3)
       on conflict do nothing`,
      [userId, companyId, key],
    );
  }
  return companyId;
}

/**
 * Never merge automatically when (Spec §09):
 *   · either application has an offer, is accepted, or is negotiating
 *   · the two have different work_mode or a location in a different country
 *   · one of them was created by hand — a user-declared application is
 *     authoritative
 *   · a previous human_corrected event split them apart before
 */
export async function mergeIsForbidden(
  sql: pg.PoolClient,
  a: CandidateRow,
  b: CandidateRow,
): Promise<string | null> {
  const terminal = ['offer', 'negotiating'];
  for (const app of [a, b]) {
    if (terminal.includes(app.current_stage)) return 'an offer or negotiation is open';
    if (app.status === 'accepted') return 'one of them was accepted';
    if (app.manually_created) return 'one of them was declared by hand';
  }
  if (a.work_mode && b.work_mode && a.work_mode !== b.work_mode) return 'different work mode';
  if (a.location && b.location && countryOf(a.location) !== countryOf(b.location)) {
    return 'different country';
  }
  const split = await sql.query(
    `select 1 from application_events
      where type = 'human_corrected'
        and payload->>'field' = 'merge'
        and (application_id = $1 or application_id = $2)
        and payload->>'to' = 'split'
      limit 1`,
    [a.id, b.id],
  );
  if (split.rowCount) return 'a human split them before';
  return null;
}

/** Crude, and deliberately so: it only has to separate Milan from Berlin. */
function countryOf(location: string): string {
  const l = location.toLowerCase();
  const map: Array<[RegExp, string]> = [
    [/milan|rome|roma|turin|napoli|bologna|italy|italia|trento|biassono/, 'it'],
    [/berlin|munich|münchen|hamburg|frankfurt|germany|deutschland/, 'de'],
    [/london|manchester|uk|united kingdom/, 'gb'],
    [/paris|lyon|france/, 'fr'],
    [/madrid|barcelona|spain/, 'es'],
    [/amsterdam|netherlands/, 'nl'],
  ];
  for (const [re, code] of map) if (re.test(l)) return code;
  return 'unknown';
}

export async function resolve(
  deps: ResolveDeps,
  signal: Signal & { application_hint?: string | null },
): Promise<Decision> {
  const { sql, embedder } = deps;
  const userId = signal.user_id;

  // ── 1 · thread identity: cheapest and strongest ──────────────────────────
  if (signal.application_hint) {
    return { kind: 'thread', applicationId: signal.application_hint, confidence: 0.99 };
  }
  if (signal.thread_id) {
    const byThread = await sql.query<{ application_id: string }>(
      `select application_id from application_events
        where user_id = $1 and payload->>'thread_id' = $2 limit 1`,
      [userId, signal.thread_id],
    );
    if (byThread.rows[0]) {
      return { kind: 'thread', applicationId: byThread.rows[0].application_id, confidence: 0.99 };
    }
  }

  // ── 2 · company canonicalisation ─────────────────────────────────────────
  const companyId = await canonicaliseCompany(deps, userId, signal);

  // Prefer the extractor's normalisation and fall back to doing it here, so a
  // signal from an older queue message still resolves.
  const normalisedRole = signal.role_normalised
    ? { ...normaliseRole(signal.role ?? 'unknown'), role: signal.role_normalised }
    : normaliseRole(signal.role ?? 'unknown');
  const embedding = await embedder.embed(normalisedRole.role || 'unknown');

  const candidatesRes = await sql.query<CandidateRow>(
    `select id, role_title, role_normalised, role_embedding::text as role_embedding,
            applied_at, status, current_stage, work_mode, location, manually_created
       from applications
      where user_id = $1 and company_id = $2 and merged_into_id is null
        and status in ('live','dormant')`,
    [userId, companyId],
  );
  const candidates = candidatesRes.rows;

  // ── 3 · role match inside the company ────────────────────────────────────
  if (candidates.length === 0) {
    return { kind: 'created', applicationId: await createApplication(deps, signal, companyId, normalisedRole, embedding) };
  }

  const scored = candidates
    .map((c) => ({ row: c, cos: cosine(embedding, parseVector(c.role_embedding) ?? []) }))
    .sort((a, b) => b.cos - a.cos);

  const best = scored[0]!;

  /**
   * A signal that names no role at all.
   *
   * Most real mail does not repeat the job title: a calendar invite says
   * "Interview with Prima", a rejection says "we will not be moving forward".
   * Embedding the placeholder "unknown" gives a cosine near zero against every
   * candidate, so the old code created a *second* application at the same
   * company for every such message — one employer became four rows, and the
   * event log for the real application lost its own rejection.
   *
   * When the signal carries no role and the company has exactly one open
   * application, that application is the only thing the message can be about.
   * With more than one candidate it stays ambiguous and asks, as it should.
   */
  const roleless = !signal.role || normalisedRole.role === 'unknown' || normalisedRole.role === '';

  if (candidates.length === 1) {
    if (roleless) {
      return { kind: 'attached', applicationId: best.row.id, cosine: 1 };
    }
    if (best.cos >= RESOLVER.ATTACH_SINGLE) {
      return { kind: 'attached', applicationId: best.row.id, cosine: best.cos };
    }
    return {
      kind: 'created',
      applicationId: await createApplication(deps, signal, companyId, normalisedRole, embedding),
    };
  }

  if (roleless) {
    // Several open applications at this company and nothing to tell them apart.
    return {
      kind: 'ambiguous',
      candidates: scored.slice(0, 3).map((s) => ({ id: s.row.id, cosine: s.cos })),
    };
  }

  const second = scored[1]!;
  if (best.cos >= RESOLVER.ATTACH_MULTI) {
    if (best.cos - second.cos >= RESOLVER.AMBIGUITY_MARGIN) {
      return { kind: 'attached', applicationId: best.row.id, cosine: best.cos };
    }
    // Two candidates within 0.05 of each other. The system does not guess here;
    // it asks, once, and writes the answer back as a rule.
    return {
      kind: 'ambiguous',
      candidates: scored.slice(0, 3).map((s) => ({ id: s.row.id, cosine: s.cos })),
    };
  }

  return {
    kind: 'created',
    applicationId: await createApplication(deps, signal, companyId, normalisedRole, embedding),
  };
}

async function createApplication(
  deps: ResolveDeps,
  signal: Signal,
  companyId: string,
  normalised: ReturnType<typeof normaliseRole>,
  embedding: number[],
): Promise<string> {
  const res = await deps.sql.query<{ id: string }>(
    `insert into applications
       (user_id, company_id, role_title, role_normalised, role_embedding,
        seniority, location, work_mode, current_stage, current_phase, confidence)
     values ($1,$2,$3,$4,$5::vector,$6,$7,$8,'applied','sent',$9)
     returning id`,
    [
      signal.user_id,
      companyId,
      signal.role?.trim() || 'Unknown role',
      normalised.role,
      toVector(embedding),
      normalised.seniority,
      signal.location ?? normalised.location,
      signal.work_mode ?? normalised.workMode,
      signal.confidence,
    ],
  );
  return res.rows[0]!.id;
}

/**
 * Cross-channel dedup, run after attach or create.
 *
 * The same job found on LinkedIn and on the company site is one application
 * with two provenances — which is the thing that makes channel statistics
 * honest in the first place.
 *
 * The merge is automatic outside the exclusions, as §09 specifies, but it is
 * made reversible: every automatic merge posts an FYI card to the review queue
 * with a one-tap undo for 14 days. Always-asking would flood the queue with
 * cases the resolver is right about; an irreversible silent merge is the
 * failure the spec fears. decisions.md D5.
 */
export async function findDuplicate(
  deps: ResolveDeps,
  userId: string,
  applicationId: string,
): Promise<{ keep: string; merge: string; cos: number } | null> {
  const { sql } = deps;
  const meRes = await sql.query<CandidateRow & { company_id: string }>(
    `select id, company_id, role_title, role_normalised, role_embedding::text as role_embedding,
            applied_at, status, current_stage, work_mode, location, manually_created
       from applications where id = $1`,
    [applicationId],
  );
  const me = meRes.rows[0];
  if (!me?.role_embedding) return null;

  const others = await sql.query<CandidateRow>(
    `select id, role_title, role_normalised, role_embedding::text as role_embedding,
            applied_at, status, current_stage, work_mode, location, manually_created
       from applications
      where user_id = $1 and company_id = $2 and id <> $3 and merged_into_id is null`,
    [userId, me.company_id, applicationId],
  );

  const mine = parseVector(me.role_embedding)!;
  for (const other of others.rows) {
    const cos = cosine(mine, parseVector(other.role_embedding) ?? []);
    if (cos < RESOLVER.DEDUP_MERGE) continue;

    if (me.applied_at && other.applied_at) {
      const days = Math.abs(me.applied_at.getTime() - other.applied_at.getTime()) / 86_400_000;
      if (days > RESOLVER.DEDUP_WINDOW_DAYS) continue;
    }

    if (await mergeIsForbidden(sql, me, other)) continue;

    // Keep the earliest as first touch — every timing statistic is measured
    // from it, and channel attribution depends on it.
    const meFirst = (me.applied_at?.getTime() ?? Infinity) <= (other.applied_at?.getTime() ?? Infinity);
    return meFirst
      ? { keep: me.id, merge: other.id, cos }
      : { keep: other.id, merge: me.id, cos };
  }
  return null;
}

/** `iongroup.com` → `iongroup`. The registrable label, not the whole host. */
function domainLabel(domain: string | null): string | null {
  if (!domain) return null;
  const parts = domain.split('.').filter(Boolean);
  if (parts.length < 2) return domain;
  // Handle two-part public suffixes like .co.uk / .com.br.
  const last = parts[parts.length - 1]!;
  const penultimate = parts[parts.length - 2]!;
  const isCompound = penultimate.length <= 3 && last.length <= 3 && parts.length >= 3;
  return isCompound ? parts[parts.length - 3]! : penultimate;
}
