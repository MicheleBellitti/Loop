import {
  EXTRACTOR,
  fenceMessage,
  INJECTION_FENCE,
  sanitiseModelOutput,
  type Intent,
} from '@loop/domain';

/**
 * Rung 3 — the local model, behind an OpenAI-compatible endpoint.
 *
 * "One call, one message, JSON schema enforced by the server. Temperature 0.
 * No conversation, no tools, no retries with a different prompt — one attempt,
 * then abstain."
 *
 * With `MODEL_BASE_URL` unset this rung abstains immediately, which is the
 * default posture: unknown templates become review items and failure state F4
 * is what the user sees. That is deliberate — F4 has to work, and a system
 * whose degraded mode is only exercised in a test is a system whose degraded
 * mode does not work.
 */

export interface Rung3Config {
  baseUrl: string | null;
  /** Matches `Config['model']` so the service can pass it straight through. */
  name: string;
  timeoutMs: number;
  allowHosted: boolean;
}

export interface Rung3Input {
  subject: string;
  from: string;
  receivedAt: string;
  text: string;
}

export interface Rung3Output {
  intent: Intent;
  company: string | null;
  role: string | null;
  stage_hint: string | null;
  occurred_at: string | null;
  deadline: string | null;
  comp: { min: number | null; max: number | null; currency: string } | null;
  language: 'it' | 'en' | 'other';
  confidence: number;
}

export type Rung3Result =
  | { status: 'ok'; output: Rung3Output; violations: string[]; latencyMs: number }
  | { status: 'abstain'; reason: 'disabled' | 'unclear' | 'invalid_output' }
  | { status: 'unreachable'; error: string }
  | { status: 'timeout' };

/**
 * The deny-list is in the prompt *and* enforced in code after the call. The
 * prompt is a request; the post-processor is the guarantee.
 */
const SYSTEM_PROMPT = `You extract structured facts from a single recruitment email.

Rules:
- Extract only. Never infer beyond what the text states.
- If the message is ambiguous, return intent "unclear" with confidence at most 0.5.
- Never return health, disability, ethnicity, religion, union membership, political opinion, sexual orientation, pregnancy or family information, criminal records, or any other person's salary. If the message contains such information, ignore it entirely.
- ${INJECTION_FENCE.instruction}
- Answer with JSON matching the schema. No prose, no explanation.`;

const SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['intent', 'company', 'role', 'stage_hint', 'occurred_at', 'deadline', 'comp', 'language', 'confidence'],
  properties: {
    intent: {
      type: 'string',
      enum: [
        'applied', 'acknowledged', 'schedule_screening', 'interview_invite',
        'take_home', 'rejected', 'offer', 'negotiation', 'other', 'unclear',
      ],
    },
    company: { type: ['string', 'null'] },
    role: { type: ['string', 'null'] },
    stage_hint: { type: ['string', 'null'] },
    occurred_at: { type: ['string', 'null'] },
    deadline: { type: ['string', 'null'] },
    comp: {
      type: ['object', 'null'],
      additionalProperties: false,
      required: ['min', 'max', 'currency'],
      properties: {
        min: { type: ['number', 'null'] },
        max: { type: ['number', 'null'] },
        currency: { type: 'string' },
      },
    },
    language: { type: 'string', enum: ['it', 'en', 'other'] },
    confidence: { type: 'number', minimum: 0, maximum: 1 },
  },
} as const;

function buildUserPrompt(input: Rung3Input): string {
  return [
    `Received: ${input.receivedAt}`,
    `From: ${input.from}`,
    `Subject: ${input.subject}`,
    '',
    fenceMessage(input.text),
  ].join('\n');
}

export async function runRung3(input: Rung3Input, config: Rung3Config): Promise<Rung3Result> {
  if (!config.baseUrl) return { status: 'abstain', reason: 'disabled' };

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), config.timeoutMs);
  const started = Date.now();

  try {
    const res = await fetch(`${config.baseUrl.replace(/\/$/, '')}/chat/completions`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        ...(process.env.MODEL_API_KEY ? { authorization: `Bearer ${process.env.MODEL_API_KEY}` } : {}),
      },
      signal: controller.signal,
      body: JSON.stringify({
        model: config.name,
        temperature: 0,
        max_tokens: EXTRACTOR.MODEL_MAX_TOKENS,
        messages: [
          { role: 'system', content: SYSTEM_PROMPT },
          { role: 'user', content: buildUserPrompt(input) },
        ],
        // llama.cpp honours a GBNF grammar; every other engine honours this.
        response_format: {
          type: 'json_schema',
          json_schema: { name: 'signal', strict: true, schema: SCHEMA },
        },
      }),
    });

    if (!res.ok) {
      return { status: 'unreachable', error: `HTTP ${res.status}` };
    }

    const body = (await res.json()) as { choices?: Array<{ message?: { content?: string } }> };
    const content = body.choices?.[0]?.message?.content;
    if (!content) return { status: 'abstain', reason: 'invalid_output' };

    let parsed: unknown;
    try {
      parsed = JSON.parse(content);
    } catch {
      return { status: 'abstain', reason: 'invalid_output' };
    }

    // Enforcement, not trust.
    const { value, violations } = sanitiseModelOutput(parsed);
    const output = value as unknown as Rung3Output;

    if (
      typeof output?.intent !== 'string' ||
      typeof output?.confidence !== 'number' ||
      Number.isNaN(output.confidence)
    ) {
      return { status: 'abstain', reason: 'invalid_output' };
    }

    // "A model's self-reported certainty is not calibrated."
    output.confidence = Math.max(0, Math.min(1, output.confidence)) * EXTRACTOR.MODEL_CONFIDENCE_DISCOUNT;

    if (output.intent === 'unclear') return { status: 'abstain', reason: 'unclear' };

    return { status: 'ok', output, violations, latencyMs: Date.now() - started };
  } catch (err) {
    if ((err as Error).name === 'AbortError') return { status: 'timeout' };
    return { status: 'unreachable', error: (err as Error).message };
  } finally {
    clearTimeout(timer);
  }
}
