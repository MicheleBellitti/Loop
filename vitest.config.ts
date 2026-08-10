import { defineConfig } from 'vitest/config';

// Two kinds of test, distinguished by filename rather than by directory so a
// module's unit and integration tests can sit next to the code they cover.
//   *.test.ts   — pure, no I/O, runs everywhere, runs on every commit
//   *.itest.ts  — needs a real Postgres via Testcontainers (the interesting
//                 bugs live in SQL and in parsing, so both need the real thing)
export default defineConfig({
  test: {
    globals: false,
    environment: 'node',
    include: ['packages/**/*.test.ts', 'services/**/*.test.ts', 'scripts/**/*.test.ts'],
    exclude: ['**/node_modules/**', '**/dist/**', 'client/**'],
    testTimeout: 10_000,
    coverage: {
      provider: 'v8',
      include: ['packages/domain/src/**'],
      thresholds: { lines: 95, functions: 95, branches: 90, statements: 95 },
    },
  },
});
