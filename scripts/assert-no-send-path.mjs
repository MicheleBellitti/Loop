#!/usr/bin/env node
import { readdir, readFile } from 'node:fs/promises';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The CI grep §12 asks for.
 *
 * "The service MUST NOT have a send path — there is no code that calls an SMTP
 * or Gmail send API anywhere in the repo, and a CI grep asserts it."
 *
 * Loop drafts follow-ups and never has the right to send one; this is the check
 * that keeps that true as the codebase grows. Web push is not mail and is
 * allowed — it is the only outbound channel, it needs no mailbox scope, and it
 * cannot reach a recruiter.
 */

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const FORBIDDEN = [
  { pattern: /\bnodemailer\b/i, why: 'SMTP client' },
  { pattern: /createTransport\s*\(/, why: 'SMTP transport' },
  { pattern: /\bsendMail\s*\(/, why: 'SMTP send' },
  { pattern: /users\.messages\.send/, why: 'Gmail send API' },
  { pattern: /gmail\.send\b/, why: 'Gmail send API' },
  { pattern: /['"`]https:\/\/api\.(sendgrid|mailgun|postmark|resend)/i, why: 'hosted mail API' },
  { pattern: /\bsmtp:\/\//i, why: 'SMTP URL' },
  { pattern: /gmail\.modify|gmail\.compose|mail\.google\.com\/["'\s]/i, why: 'a write scope' },
];

const SKIP_DIRS = new Set(['node_modules', 'dist', '.git', 'design', 'coverage', 'fixtures']);
const EXTENSIONS = /\.(ts|tsx|js|mjs|cjs|jsx|yaml|yml|sql|json)$/;

/** This file names every pattern it forbids, so it exempts itself. */
const SELF = relative(ROOT, fileURLToPath(import.meta.url));

async function* walk(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue;
      yield* walk(join(dir, entry.name));
    } else if (EXTENSIONS.test(entry.name)) {
      yield join(dir, entry.name);
    }
  }
}

const violations = [];
for await (const file of walk(ROOT)) {
  const rel = relative(ROOT, file);
  if (rel === SELF) continue;
  const text = await readFile(file, 'utf8');
  const lines = text.split('\n');
  for (const [i, line] of lines.entries()) {
    // A line that only talks *about* the rule is not a send path.
    if (/MUST NOT|never sends?|cannot send|no send path|read-only/i.test(line)) continue;
    for (const { pattern, why } of FORBIDDEN) {
      if (pattern.test(line)) violations.push({ file: rel, line: i + 1, why, text: line.trim() });
    }
  }
}

if (violations.length > 0) {
  console.error('\n  A send path reached the repository. Loop drafts follow-ups; it never sends one.\n');
  for (const v of violations) {
    console.error(`  ${v.file}:${v.line}  ${v.why}`);
    console.error(`      ${v.text.slice(0, 100)}`);
  }
  console.error('');
  process.exit(1);
}

console.log('no send path — Loop can draft a follow-up and cannot deliver one');
