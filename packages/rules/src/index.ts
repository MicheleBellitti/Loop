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
  company_from: z.enum(['subject_capture', 'sender_domain', 'body_capture']).default('subject_capture'),
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
 * Apply the registry to one message. Returns the first pattern that matches,
 * or null — a rung MUST abstain rather than guess, so an unmatched vendor falls
 * through to rung 2 instead of producing a low-confidence intent.
 */
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

      let company: string | null = fields.company ?? null;
      if (!company && rule.company_from === 'sender_domain') {
        // Strip the vendor's own domain: an ATS is never the employer.
        company = senderDomain.split('.').slice(0, -1).join('.') || null;
      }

      return {
        vendor: rule.vendor,
        intent: p.intent,
        confidence: p.confidence,
        company,
        role: fields.role ?? null,
        fields,
        patternIndex: i,
      };
    }
    // The vendor is known but no pattern fits: this is an unknown template, and
    // saying so is more useful than pretending the whole message is unknown.
    return null;
  }
  return null;
}
