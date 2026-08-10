import { EXTRACTOR } from './thresholds.js';

/**
 * Shared pre-processing, applied before any rung sees a message.
 *
 * "HTML → text (strip style, script, tracking pixels, and any img), remove
 * quoted history (> blocks, On … wrote:, Il … ha scritto:), collapse
 * whitespace, cap at 6 000 characters." (Spec §08)
 *
 * Done by hand rather than with a sanitiser library because the output is not
 * HTML — it is plain text for a regex and a model — and because a dependency
 * that renders untrusted HTML is a larger attack surface than forty lines of
 * tag stripping that never executes anything.
 */

const BLOCK_TAGS = /<\/?(p|div|br|tr|li|h[1-6]|table|blockquote|section|article)[^>]*>/gi;

export function htmlToText(html: string): string {
  return (
    html
      // Anything that can execute or load goes first, contents and all.
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
      .replace(/<head\b[^>]*>[\s\S]*?<\/head>/gi, ' ')
      .replace(/<!--[\s\S]*?-->/g, ' ')
      // Tracking pixels and every other image: a 1×1 GIF is a read receipt, and
      // an alt text is never worth the request.
      .replace(/<img\b[^>]*>/gi, ' ')
      // Keep the href so a posting URL survives, drop the anchor markup.
      .replace(/<a\b[^>]*href=["']([^"']+)["'][^>]*>([\s\S]*?)<\/a>/gi, (_m, href: string, label: string) => {
        const text = label.replace(/<[^>]+>/g, '').trim();
        return text && !/^https?:/i.test(text) ? `${text} <${href}>` : ` ${href} `;
      })
      .replace(BLOCK_TAGS, '\n')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/gi, ' ')
      .replace(/&amp;/gi, '&')
      .replace(/&lt;/gi, '<')
      .replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"')
      .replace(/&#(\d+);/g, (_m, code: string) => String.fromCharCode(Number(code)))
  );
}

/**
 * Quoted history.
 *
 * A recruiter thread accumulates the entire conversation on every reply, and
 * leaving it in means rung 3 reads a rejection from three months ago as today's
 * news — and pays for the tokens.
 */
const QUOTE_MARKERS: RegExp[] = [
  /^\s*On .{5,120}\s+wrote:\s*$/im,
  /^\s*Il .{5,120}\s+ha scritto:\s*$/im,
  /^\s*-{2,}\s*Original Message\s*-{2,}\s*$/im,
  /^\s*-{2,}\s*Messaggio originale\s*-{2,}\s*$/im,
  /^\s*From:\s.+\n\s*Sent:\s/im,
  /^\s*Da:\s.+\n\s*Inviato:\s/im,
  /^_{10,}\s*$/m,
];

export function stripQuotedHistory(text: string): string {
  let cut = text.length;
  for (const re of QUOTE_MARKERS) {
    const m = re.exec(text);
    if (m && m.index < cut) cut = m.index;
  }
  const head = text.slice(0, cut);
  // Whatever survives may still carry a > block at the bottom.
  const lines = head.split('\n');
  let end = lines.length;
  while (end > 0 && /^\s*>/.test(lines[end - 1] ?? '')) end -= 1;
  return lines.slice(0, end).join('\n');
}

/** Signature blocks add noise and, occasionally, an injection attempt. */
const SIGNATURE = /^\s*--\s*$/m;

export function stripSignature(text: string): string {
  const m = SIGNATURE.exec(text);
  return m ? text.slice(0, m.index) : text;
}

export interface Normalised {
  text: string;
  /** Links found before the text was capped — a posting URL often sits late. */
  links: string[];
  truncated: boolean;
}

const URL_RE = /https?:\/\/[^\s<>"')]+/gi;

export function normaliseMessage(input: { text?: string; html?: string }): Normalised {
  const raw = input.html ? htmlToText(input.html) : (input.text ?? '');
  const links = [...new Set(raw.match(URL_RE) ?? [])].slice(0, 20);

  let text = stripSignature(stripQuotedHistory(raw))
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t ]+/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  const truncated = text.length > EXTRACTOR.MAX_TEXT_CHARS;
  if (truncated) text = text.slice(0, EXTRACTOR.MAX_TEXT_CHARS);

  return { text, links, truncated };
}

/** ≤280 chars, whole words, for a review card. The only text ever persisted. */
export function excerpt(text: string, max = 280): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  if (flat.length <= max) return flat;
  const cut = flat.slice(0, max);
  const lastSpace = cut.lastIndexOf(' ');
  return `${cut.slice(0, lastSpace > max * 0.6 ? lastSpace : max)}…`;
}

/** Rough language detection — enough to pick a follow-up template. */
export function detectLanguage(text: string): 'it' | 'en' | 'other' {
  const it = (text.match(/\b(il|la|per|non|che|della|candidatura|colloquio|grazie|cordiali|saluti)\b/gi) ?? []).length;
  const en = (text.match(/\b(the|for|your|with|application|interview|thanks|regards|we|have)\b/gi) ?? []).length;
  if (it === 0 && en === 0) return 'other';
  return it > en ? 'it' : 'en';
}
