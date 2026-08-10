import { mkdir, readdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * `npm run anonymise -- ~/exported-mail`
 *
 * "Build fixtures/ from real mail on day one: anonymise names, addresses and
 * links with a script, keep the structure byte-for-byte."
 *
 * Structure is what the rules match on — the sender domain, the subject shape,
 * the header set — so it survives verbatim. What identifies a person does not:
 * addresses, display names, links and long digit runs are replaced with stable
 * pseudonyms, so a thread still reads as one thread.
 *
 * Output goes to fixtures/private/, which is git-ignored. Your inbox is the
 * only place the go/no-go number can be measured, and it is also the one thing
 * that must never leave your machine.
 */

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = process.argv[2];

if (!source) {
  console.error('usage: npm run anonymise -- /path/to/eml/directory');
  process.exit(1);
}

/** Stable per run of the same input: the same address maps to the same alias. */
const alias = (value: string, prefix: string): string =>
  `${prefix}-${createHash('sha256').update(value.toLowerCase()).digest('hex').slice(0, 8)}`;

/** Domains that are the whole signal, and therefore must not be rewritten. */
const KEEP_DOMAINS = [
  'greenhouse-mail.io', 'greenhouse.io', 'lever.co', 'myworkday.com', 'workday.com',
  'ashbyhq.com', 'smartrecruiters.com', 'workablemail.com', 'workable.com',
  'icims.com', 'taleo.net', 'recruitee.com', 'bamboohr.com',
  'linkedin.com', 'indeed.com', 'indeedemail.com',
];

const keep = (domain: string): boolean =>
  KEEP_DOMAINS.some((d) => domain === d || domain.endsWith(`.${d}`));

function anonymise(raw: string): string {
  let out = raw;

  // Addresses: keep the domain when it carries the vendor signal, alias it
  // otherwise; always alias the local part.
  out = out.replace(/([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})/g, (_m, local: string, domain: string) => {
    const d = domain.toLowerCase();
    return `${alias(local, 'user')}@${keep(d) ? d : `${alias(d, 'company')}.example`}`;
  });

  // Display names in From/To/Cc, which are usually a real person.
  out = out.replace(/^(From|To|Cc|Reply-To):\s*"?([^"<\n]+)"?\s*</gim, (_m, header: string, name: string) =>
    `${header}: ${alias(name.trim(), 'name')} <`,
  );

  // URLs: keep the host when it is a vendor, drop every path and query, since a
  // tracking link is a person's identity in a query string.
  out = out.replace(/https?:\/\/([^\s"'<>)]+)/g, (_m, rest: string) => {
    const [host = ''] = rest.split('/');
    const bare = host.toLowerCase();
    return keep(bare) ? `https://${bare}/${alias(rest, 'path')}` : `https://${alias(bare, 'host')}.example/${alias(rest, 'path')}`;
  });

  // Long digit runs: phone numbers, reference ids, anything that identifies.
  out = out.replace(/\b\d{6,}\b/g, (m) => alias(m, 'id').replace(/\D/g, '').padEnd(6, '0').slice(0, m.length));

  return out;
}

const target = join(ROOT, 'fixtures', 'private');
await mkdir(target, { recursive: true });

const files = (await readdir(source)).filter((f) => f.endsWith('.eml'));
let written = 0;
for (const file of files) {
  const raw = await readFile(join(source, file), 'utf8');
  await writeFile(join(target, basename(file)), anonymise(raw), 'utf8');
  written += 1;
}

console.log(`
  anonymised ${written} message(s) into fixtures/private/

  Read a few before you trust it. Then add the expected intent for each one to
  fixtures/manifest.json and run:

      npm run test:corpus

  The gate that decides whether Loop is worth building is measured here:
  ≥0.85 application-level recall over twelve months, with zero wrong merges.
`);
