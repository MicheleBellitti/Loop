import { readFile, readdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parse } from 'yaml';
import { z } from 'zod';
import { domainOfAddress, matchesDomainSuffix, type Intent, type RawMessage } from '@loop/domain';

/**
 * Rung 1: the ATS template registry.
 *
 * "One rule covers thousands of companies." Rules are data, reviewed like code:
 * a declarative YAML file per vendor, every pattern shipping with a fixture,
 * and CI running the whole corpus on every commit.
 */

const PatternSchema = z.object({
  intent: z.enum([
    'applied', 'acknowledged', 'schedule_screening', 'interview_invite',
    'interview_cancelled', 'take_home', 'rejected', 'offer', 'negotiation',
    // `other` lets a rule say "this vendor sent this, and it is not about an
    // application" — webinar invitations, job alerts, newsletters. Without it
    // those fall through to the model, which is asked to guess about mail a
    // rule already recognises perfectly well as noise. The domain's Intent
    // union always had it; this enum had drifted from it.
    'other',
  ]),
  subject: z.string().optional(),
  body: z.string().optional(),
  /** Named capture groups feed the extracted fields. */
  extract: z.record(z.string()).optional(),
  confidence: z.number().min(0).max(1),
  /** Restrict this pattern to one language when the vendor sends both. */
  locale: z.enum(['it', 'en']).optional(),
});

const RuleSchema = z.object({
  vendor: z.string(),
  match: z.object({
    sender_domains: z.array(z.string()).min(1),
    headers: z.record(z.string().nullable()).optional(),
  }),
  patterns: z.array(PatternSchema).min(1),
  locale: z.array(z.enum(['it', 'en'])).default(['en']),
  /**
   * Where the employer's name comes from.
   *
   * `sender_display_name` is the reliable one and it is the default for every
   * ATS, because an ATS sends *on behalf of* the employer and puts the employer
   * in the From display name: `Prima <no-reply@hire.eu.lever.co>`,
   * `Lexroom Hiring Team <no-reply@ashbyhq.com>`. Subject lines, by contrast,
   * put whatever they like in that slot — Lever's "Thanks for applying to
   * Machine Learning Engineer, here is a link to manage your application data"
   * has the *role* where the company would be, and a subject capture there
   * produced an application filed under the name of a sentence fragment.
   */
  company_from: z
    .enum(['sender_display_name', 'subject_capture', 'sender_domain', 'body_capture'])
    .default('sender_display_name'),
  /** The company is not the ATS: these domains never name a company. */
  tests: z
    .array(z.object({ fixture: z.string(), expect: z.record(z.unknown()) }))
    .default([]),
});

export type AtsPattern = z.infer<typeof PatternSchema>;
export type AtsRule = z.infer<typeof RuleSchema>;

export interface RuleMatch {
  vendor: string;
  intent: Intent;
  confidence: number;
  company: string | null;
  role: string | null;
  fields: Record<string, string>;
  patternIndex: number;
}

const HERE = dirname(fileURLToPath(import.meta.url));
const DEFAULT_DIR = join(HERE, '..', '..', '..', 'rules', 'ats');

let cache: AtsRule[] | null = null;

export async function loadRules(dir = DEFAULT_DIR): Promise<AtsRule[]> {
  const files = (await readdir(dir)).filter((f) => f.endsWith('.yaml') || f.endsWith('.yml')).sort();
  const rules: AtsRule[] = [];
  for (const f of files) {
    const raw = parse(await readFile(join(dir, f), 'utf8'));
    const parsed = RuleSchema.safeParse(raw);
    if (!parsed.success) {
      throw new Error(`rules/ats/${f} is invalid: ${parsed.error.issues.map((i) => `${i.path.join('.')}: ${i.message}`).join('; ')}`);
    }
    rules.push(parsed.data);
  }
  return rules;
}

export async function rules(dir?: string): Promise<AtsRule[]> {
  cache ??= await loadRules(dir);
  return cache;
}

export function resetRuleCache(): void {
  cache = null;
}

/** Every sending domain in the registry. The classifier's +3 lives on this. */
export function atsDomains(all: readonly AtsRule[]): string[] {
  return [...new Set(all.flatMap((r) => r.match.sender_domains))];
}

export function vendorForDomain(all: readonly AtsRule[], domain: string | null): string | null {
  if (!domain) return null;
  for (const r of all) {
    if (r.match.sender_domains.some((d) => matchesDomainSuffix(domain, d))) return r.vendor;
  }
  return null;
}

function headersMatch(rule: AtsRule, msg: RawMessage): boolean {
  const wanted = rule.match.headers;
  if (!wanted) return true;
  const present = msg.headers as unknown as Record<string, unknown>;
  return Object.entries(wanted).every(([k, v]) => {
    const actual = present[k.toLowerCase()];
    // `null` in the YAML means "the header must simply be present".
    return v === null ? actual !== undefined : typeof actual === 'string' && actual.includes(v);
  });
}

function firstNamedGroup(re: RegExp, text: string, name: string): string | null {
  const m = re.exec(text);
  return (m?.groups?.[name] ?? null) || null;
}

/**
 * The role, from the body, when the subject did not carry it.
 *
 * A job title is stated once — in the confirmation that the application was
 * received — and then never again. Every follow-up on that thread says "your
 * application" and assumes you remember which one. So a registry that only
 * reads subjects records the title for the handful of vendors that put it
 * there and leaves every other application as "Unknown role", which is not a
 * gap in the data: it is a gap in reading it.
 *
 * Both languages, ordered most-specific first. Deliberately conservative — the
 * damage a greedy capture does here is a company or a job called "here is a
 * link to manage your application data", so a candidate has to look like a job
 * title before it is believed.
 */
const ROLE_PATTERNS: RegExp[] = [
  // English
  /\bapplication\s+for\s+the\s+(?:position\s+of\s+|role\s+of\s+)?(?<role>[^.,\n<>|]{3,60}?)\s*(?:position|role|opening|vacancy)?\s*(?:[.,\n]|at\b|with\b|$)/i,
  /\bapplied\s+(?:to|for)\s+(?:the\s+)?(?<role>[^.,\n<>|]{3,60}?)\s*(?:position|role|opening)\b/i,
  /\bapply(?:ing)?\s+for\s+the\s+(?:position|role)\s+of\s+(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|at\b|with\b|$)/i,
  /\b(?:position|role)\s+of\s+(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|at\b|presso\b|$)/i,
  /\byour\s+(?:candidacy|application)\s+(?:for|as)\s+(?:the\s+)?(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|at\b|$)/i,
  /^\s*(?:position|role|job\s+title)\s*:\s*(?<role>[^\n]{3,60})$/im,
  /\binterview\s+for\s+the\s+(?<role>[^.,\n<>|]{3,60}?)\s*(?:position|role)\b/i,
  // Italian
  /\bcandidatura\s+(?:per|come)\s+(?:la\s+posizione\s+di\s+|il\s+ruolo\s+di\s+)?(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|in\b|$)/i,
  /\bposizione\s+di\s+(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|$)/i,
  /^\s*(?:posizione|ruolo)\s*:\s*(?<role>[^\n]{3,60})$/im,
  /\bcolloquio\s+(?:per|come)\s+(?:la\s+posizione\s+di\s+)?(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|$)/i,
  /\bopportunità\s+(?:di|come)\s+(?<role>[^.,\n<>|]{3,60}?)\s*(?:[.,\n]|presso\b|$)/i,
];

/** Words that make a phrase plausibly a job rather than a sentence. */
const ROLE_VOCABULARY =
  /engineer|developer|scientist|manager|analyst|designer|consultant|architect|specialist|researcher|intern|lead|director|programmer|administrator|technician|ingegner|sviluppat|analista|consulente|responsabile|stagista|tirocinan|progettista|tecnico|ricercator/i;

/**
 * Cross-vendor body patterns, tried when the vendor is known but none of its
 * own templates fit.
 *
 * This is the safest place in the whole ladder for a generic pattern: the
 * sender is already established as an ATS writing to this user about an
 * application, so the only question left is *which* kind of message it is.
 * Without it, a vendor's rule file has to enumerate every phrasing every one of
 * its customers uses in every language — Ashby delivers rejections written in
 * Italian by an Italian company, and an English-only rule file simply never
 * sees them.
 */
const GENERIC_BODY: Array<{ intent: AtsPattern['intent']; re: RegExp; confidence: number }> = [
  // Rejections. The most common message in any job search, and the one whose
  // absence leaves an application sitting "live" forever.
  {
    intent: 'rejected',
    confidence: 0.95,
    re: /\b(?:not\s+(?:be\s+)?mov(?:e|ing)\s+forward|decision\s+to\s+not\s+move\s+forward|moving\s+forward\s+with\s+other|other\s+candidates|decided\s+not\s+to\s+proceed|will\s+not\s+be\s+progressing|unable\s+to\s+offer\s+you|not\s+(?:been\s+)?select(?:ed)?)\b/i,
  },
  {
    intent: 'rejected',
    confidence: 0.95,
    re: /\b(?:non\s+(?:siamo|sarà|possiamo)\s+(?:in\s+grado\s+)?(?:di\s+)?(?:proceder|prosegui|dar\s+seguito)|abbiamo\s+deciso\s+di\s+non\s+proceder|non\s+proseguir(?:e|emo)|non\s+è\s+stata\s+selezionata|ti\s+terremo\s+in\s+considerazione\s+per\s+future|non\s+abbiamo\s+individuato|purtroppo\s+(?:non|la\s+tua))\b/i,
  },
  // Acknowledgements.
  {
    intent: 'acknowledged',
    confidence: 0.94,
    re: /\b(?:thank\s+you\s+for\s+(?:applying|your\s+application)|we\s+(?:have\s+)?received\s+your\s+application|appreciate\s+your\s+interest\s+in\s+joining)\b/i,
  },
  {
    intent: 'acknowledged',
    confidence: 0.94,
    re: /\b(?:grazie\s+per\s+(?:la\s+tua\s+candidatura|averci\s+inviato|il\s+tuo\s+interesse)|abbiamo\s+ricevuto\s+la\s+tua\s+candidatura)\b/i,
  },
  // Invitations.
  {
    intent: 'interview_invite',
    confidence: 0.9,
    re: /\b(?:would\s+like\s+to\s+invite\s+you|invite\s+you\s+to\s+(?:an?\s+)?(?:interview|call)|schedule\s+(?:a|an)\s+(?:call|interview|chat)|next\s+step\s+in\s+(?:the|our)\s+process)\b/i,
  },
  {
    intent: 'interview_invite',
    confidence: 0.9,
    re: /\b(?:vorremmo\s+invitarti|fissare\s+un\s+colloquio|organizzare\s+un(?:\s+breve)?\s+(?:colloquio|call)|disponibilità\s+per\s+un\s+colloquio)\b/i,
  },
  {
    intent: 'take_home',
    confidence: 0.9,
    re: /\b(?:take[-\s]home|coding\s+(?:exercise|challenge)|technical\s+assignment|prova\s+tecnica|esercizio\s+tecnico)\b/i,
  },
];

export function roleFromBody(text: string): string | null {
  for (const re of ROLE_PATTERNS) {
    const found = firstNamedGroup(re, text, 'role');
    if (!found) continue;
    const role = found.replace(/\s+/g, ' ').trim().replace(/[-–—:;,]+$/, '').trim();
    if (role.length < 3 || role.length > 60) continue;
    if (/[@]|https?:|\bunsubscribe\b|\bclick\b/i.test(role)) continue;
    // Six words is a generous ceiling for a job title and a low one for a
    // sentence, which is exactly the discrimination needed here.
    if (role.split(/\s+/).length > 6) continue;
    if (!ROLE_VOCABULARY.test(role)) continue;
    return role;
  }
  return null;
}

/**
 * Apply the registry to one message. Returns the first pattern that matches,
 * or null — a rung MUST abstain rather than guess, so an unmatched vendor falls
 * through to rung 2 instead of producing a low-confidence intent.
 */
/**
 * The employer out of a From display name.
 *
 * ATS mail is addressed from the company but delivered by the vendor, so the
 * display name carries the employer with a recruiting suffix bolted on:
 * "Lexroom Hiring Team", "Air Apps Recruiting", "Prima via Lever". Those
 * suffixes are the only thing standing between the header and a clean company,
 * so they come off — and anything that is plainly not a company (a person's
 * name at a personal address, a bare "no-reply") returns null so the caller
 * falls back rather than inventing an employer.
 */
const RECRUITING_SUFFIX =
  /\s*(?:\b(?:hiring|recruiting|recruitment|talent(?:\s+acquisition)?|careers?|jobs?|people(?:\s+ops)?|hr)\b\s*)*(?:\bteam\b)?\s*$/i;
const VIA_VENDOR = /\s+via\s+\w+\s*$/i;

export function companyFromDisplayName(from: string): string | null {
  const raw = /^\s*"?([^"<]+?)"?\s*</.exec(from)?.[1]?.trim();
  if (!raw) return null;
  let name = raw.replace(VIA_VENDOR, '').replace(RECRUITING_SUFFIX, '').trim();
  // "Careers @ Jet" and "Recruiting | Acme": the employer is what follows.
  name = name.replace(/^(?:careers?|jobs?|recruit(?:ing|ment)|talent|hr|people)\s*[@|·:–—-]\s*/i, '').trim();
  name = name.replace(/[|·–—-]\s*$/, '').trim();
  if (!name) return null;
  // A run-together robot name — "noreplyHRrecruitingTeam" — has no spaces to
  // put a word boundary against, so the suffix stripping above cannot see it.
  // It is not a company, and guessing one from it is worse than admitting none.
  if (!/\s/.test(name) && /(?:noreply|donotreply|recruiting|hrteam|talentteam)/i.test(name)) {
    return null;
  }
  // A bare mail-robot name is not a company.
  if (/^(no[-\s._]?reply|do[-\s._]?not[-\s._]?reply|notifications?|support|info|admin)$/i.test(name)) {
    return null;
  }
  return name;
}

export function applyRules(all: readonly AtsRule[], msg: RawMessage): RuleMatch | null {
  const senderDomain = domainOfAddress(msg.headers.from);
  if (!senderDomain) return null;

  for (const rule of all) {
    if (!rule.match.sender_domains.some((d) => matchesDomainSuffix(senderDomain, d))) continue;
    if (!headersMatch(rule, msg)) continue;

    for (const [i, p] of rule.patterns.entries()) {
      let subjectMatch: RegExpExecArray | null = null;
      if (p.subject) {
        subjectMatch = new RegExp(p.subject, 'i').exec(msg.headers.subject);
        if (!subjectMatch) continue;
      }
      if (p.body && !new RegExp(p.body, 'i').test(msg.text)) continue;
      if (!p.subject && !p.body) continue;

      const fields: Record<string, string> = {};
      for (const [k, v] of Object.entries(subjectMatch?.groups ?? {})) {
        if (v) fields[k] = v.trim();
      }
      for (const [field, pattern] of Object.entries(p.extract ?? {})) {
        const found = firstNamedGroup(new RegExp(pattern, 'i'), `${msg.headers.subject}\n${msg.text}`, field);
        if (found) fields[field] = found.trim();
      }

      let company: string | null = null;
      if (rule.company_from === 'sender_display_name') {
        company = companyFromDisplayName(msg.headers.from) ?? fields.company ?? null;
      } else {
        company = fields.company ?? null;
        if (!company && rule.company_from === 'sender_domain') {
          // Strip the vendor's own domain: an ATS is never the employer.
          company = senderDomain.split('.').slice(0, -1).join('.') || null;
        }
        if (!company) company = companyFromDisplayName(msg.headers.from);
      }

      // The subject rarely names the job; the confirmation body usually does.
      const role = fields.role ?? roleFromBody(`${msg.headers.subject}\n${msg.text}`);

      return {
        vendor: rule.vendor,
        intent: p.intent,
        confidence: p.confidence,
        company,
        role,
        fields,
        patternIndex: i,
      };
    }
    // The vendor is known but none of its own templates fit. Before giving up,
    // try the cross-vendor body vocabulary: the sender is established, so the
    // only open question is which kind of message this is.
    const haystack = `${msg.headers.subject}\n${msg.text}`;
    for (const g of GENERIC_BODY) {
      if (!g.re.test(haystack)) continue;
      return {
        vendor: rule.vendor,
        intent: g.intent,
        confidence: g.confidence,
        company: companyFromDisplayName(msg.headers.from),
        role: roleFromBody(haystack),
        fields: {},
        patternIndex: -1,
      };
    }
    return null;
  }
  return null;
}


/**
 * The generic body vocabulary, exposed so rung 2 can apply it to senders that
 * are not in the ATS registry — see services/extractor/src/rung2.ts for why
 * that is a deliberate widening rather than a shortcut around the model.
 */
export function matchGenericBody(
  text: string,
): { intent: AtsPattern['intent']; confidence: number } | null {
  for (const g of GENERIC_BODY) {
    if (g.re.test(text)) return { intent: g.intent, confidence: g.confidence };
  }
  return null;
}
