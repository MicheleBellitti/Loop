import { runCorpus, summarise } from './corpus.js';

/**
 * `npm run test:corpus`
 *
 * Prints the confusion matrix and enforces the merge gate from §17:
 *   intent precision ≥ 0.97, recall ≥ 0.90
 *   zero false negatives on the LinkedIn/Indeed fixtures
 *
 * This is also the table §09 asks to see in any PR that moves a threshold.
 */

const results = await runCorpus();
const report = summarise(results);

const pct = (v: number): string => `${(v * 100).toFixed(1)}%`;

console.log('\n  corpus\n  ──────');
console.log(`  cases      ${report.total}`);
console.log(`  passing    ${report.passed}`);
if (report.deferredToModel > 0) {
  console.log(
    `  deferred   ${report.deferredToModel}   (need rung 3; with the model off they become review items — failure state F4)`,
  );
}
console.log(`  precision  ${pct(report.precision)}   (gate ≥ 97.0%)`);
console.log(`  recall     ${pct(report.recall)}   (gate ≥ 90.0%)`);

console.log('\n  by intent');
const width = Math.max(...[...report.byIntent.keys()].map((k) => k.length));
for (const [intent, row] of [...report.byIntent].sort()) {
  const bar = '█'.repeat(Math.round((row.correct / Math.max(1, row.expected)) * 20));
  console.log(
    `  ${intent.padEnd(width)}  ${String(row.correct).padStart(3)}/${String(row.expected).padEnd(3)}  ${bar}`,
  );
}

if (report.failures.length > 0) {
  console.log('\n  failures');
  for (const f of report.failures) console.log(`  · ${f.file}\n      ${f.why}`);
}

let failed = false;

// "Dropping a real application is invisible and unrecoverable."
if (report.falseNegatives.length > 0) {
  console.error('\n  FALSE NEGATIVES — the classifier dropped mail it must keep:');
  for (const f of report.falseNegatives) console.error(`  · ${f.file}`);
  failed = true;
}

if (report.precision < 0.97) {
  console.error(`\n  precision ${pct(report.precision)} is below the 97% merge gate`);
  failed = true;
}
if (report.recall < 0.9) {
  console.error(`\n  recall ${pct(report.recall)} is below the 90% merge gate`);
  failed = true;
}

console.log('');
process.exit(failed ? 1 : 0);
