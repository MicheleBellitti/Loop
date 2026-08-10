import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';

/**
 * Quick-add fetches a URL the user pasted, which makes the gateway an
 * open-ended HTTP client sitting inside a private network. §14 states the
 * defence: "resolves DNS first and refuses private ranges, redirects > 3, and
 * non-HTML content types."
 *
 * The DNS resolution happens *before* the request and the request is then made
 * against the resolved address, so a name that answers publicly on the first
 * lookup and privately on the second cannot slip through.
 */

const MAX_REDIRECTS = 3;
const MAX_BYTES = 512 * 1024;
const TIMEOUT_MS = 8_000;

export class BlockedUrl extends Error {}

function isPrivateV4(ip: string): boolean {
  const p = ip.split('.').map(Number);
  const [a, b] = p as [number, number];
  return (
    a === 0 || a === 10 || a === 127 ||
    (a === 169 && b === 254) ||        // link-local, incl. cloud metadata
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127) || // carrier-grade NAT
    a >= 224                              // multicast and reserved
  );
}

function isPrivateV6(ip: string): boolean {
  const v = ip.toLowerCase();
  return (
    v === '::' || v === '::1' ||
    v.startsWith('fc') || v.startsWith('fd') ||  // unique local
    v.startsWith('fe80') ||                       // link-local
    v.startsWith('::ffff:')                       // v4-mapped: check the v4 part
  );
}

export async function assertPublicUrl(raw: string): Promise<URL> {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new BlockedUrl('not a URL');
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new BlockedUrl('only http and https are fetched');
  }

  const host = url.hostname.replace(/^\[|\]$/g, '');
  const addresses = isIP(host)
    ? [{ address: host, family: isIP(host) }]
    : await lookup(host, { all: true }).catch(() => {
        throw new BlockedUrl('host does not resolve');
      });

  for (const a of addresses) {
    const mapped = a.address.toLowerCase().startsWith('::ffff:') ? a.address.slice(7) : a.address;
    const bad = isIP(mapped) === 4 ? isPrivateV4(mapped) : isPrivateV6(a.address);
    if (bad) throw new BlockedUrl(`refuses to fetch a private address (${a.address})`);
  }
  return url;
}

export interface FetchedPage {
  url: string;
  html: string;
}

/**
 * Best-effort metadata fetch. "With a URL, metadata is fetched best-effort and
 * never blocks the 201" — so every failure here is swallowed by the caller and
 * the application is created regardless.
 */
export async function fetchPostingHtml(raw: string): Promise<FetchedPage> {
  let current = await assertPublicUrl(raw);

  for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(current, {
        redirect: 'manual',
        signal: controller.signal,
        headers: { accept: 'text/html', 'user-agent': 'Loop/1.0 (+self-hosted application tracker)' },
      });

      if (res.status >= 300 && res.status < 400) {
        const location = res.headers.get('location');
        if (!location) throw new BlockedUrl('redirect without a location');
        if (hop === MAX_REDIRECTS) throw new BlockedUrl('too many redirects');
        current = await assertPublicUrl(new URL(location, current).toString());
        continue;
      }

      const type = res.headers.get('content-type') ?? '';
      if (!/^text\/html|^application\/xhtml/.test(type)) {
        throw new BlockedUrl(`refuses a non-HTML response (${type || 'unknown'})`);
      }

      const reader = res.body?.getReader();
      if (!reader) throw new BlockedUrl('empty response');
      const chunks: Uint8Array[] = [];
      let total = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        total += value.length;
        if (total > MAX_BYTES) {
          await reader.cancel();
          break;
        }
        chunks.push(value);
      }
      return { url: current.toString(), html: Buffer.concat(chunks).toString('utf8') };
    } finally {
      clearTimeout(timer);
    }
  }
  throw new BlockedUrl('too many redirects');
}

export interface PostingMetadata {
  company: string | null;
  role: string | null;
  location: string | null;
  ats_vendor: string | null;
  comp: { min_minor: number; max_minor: number | null; currency: string } | null;
}

const ATS_HINTS: Array<[RegExp, string]> = [
  [/greenhouse\.io|greenhouse-mail/i, 'greenhouse'],
  [/lever\.co/i, 'lever'],
  [/myworkday|workday/i, 'workday'],
  [/ashbyhq/i, 'ashby'],
  [/smartrecruiters/i, 'smartrecruiters'],
  [/workable/i, 'workable'],
  [/icims/i, 'icims'],
  [/taleo|oraclecloud/i, 'taleo'],
  [/recruitee/i, 'recruitee'],
  [/bamboohr/i, 'bamboohr'],
];

const meta = (html: string, prop: string): string | null =>
  new RegExp(`<meta[^>]+(?:property|name)=["']${prop}["'][^>]+content=["']([^"']+)["']`, 'i').exec(html)?.[1] ?? null;

/** Structured data first, then Open Graph, then the title. Never invents. */
export function parsePosting(page: FetchedPage): PostingMetadata {
  const { html, url } = page;
  const jsonLd = /<script[^>]+application\/ld\+json[^>]*>([\s\S]*?)<\/script>/i.exec(html)?.[1];
  let company: string | null = null;
  let role: string | null = null;
  let location: string | null = null;
  let comp: PostingMetadata['comp'] = null;

  if (jsonLd) {
    try {
      const data = JSON.parse(jsonLd) as Record<string, unknown>;
      const posting = (Array.isArray(data) ? data[0] : data) as Record<string, unknown>;
      role = (posting.title as string) ?? null;
      company = ((posting.hiringOrganization as Record<string, unknown>)?.name as string) ?? null;
      const loc = (posting.jobLocation as Record<string, unknown>)?.address as Record<string, unknown>;
      location = (loc?.addressLocality as string) ?? null;
      const salary = (posting.baseSalary as Record<string, unknown>)?.value as Record<string, unknown>;
      if (salary?.minValue) {
        comp = {
          min_minor: Math.round(Number(salary.minValue) * 100),
          max_minor: salary.maxValue ? Math.round(Number(salary.maxValue) * 100) : null,
          currency: ((posting.baseSalary as Record<string, unknown>)?.currency as string) ?? 'EUR',
        };
      }
    } catch {
      // Structured data that does not parse is simply absent.
    }
  }

  role ??= meta(html, 'og:title') ?? /<title>([^<]+)<\/title>/i.exec(html)?.[1]?.trim() ?? null;
  company ??= meta(html, 'og:site_name');

  const vendor = ATS_HINTS.find(([re]) => re.test(url) || re.test(html))?.[1] ?? null;
  return { company, role, location, ats_vendor: vendor, comp };
}
