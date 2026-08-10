import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['packages/**/*.itest.ts', 'services/**/*.itest.ts'],
    exclude: ['**/node_modules/**', '**/dist/**'],
    // A container start plus migrations is slow; the suite is serial because
    // every test shares one database and RLS session state.
    testTimeout: 180_000,
    hookTimeout: 180_000,
    fileParallelism: false,
  },
});
