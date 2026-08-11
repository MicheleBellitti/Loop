import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The client is built, not templated — which is what makes a CSP without
 * `unsafe-inline` possible (Engineering Spec §14). Static files behind Caddy,
 * no SSR, and a service worker for offline reads and push.
 */
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'dist',
    // Modern targets only: this ships to an installed PWA on a phone the user
    // owns, not to the open web.
    target: 'es2022',
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:3000', changeOrigin: true },
      // `/health/deep` is the one gateway route the client calls without an
      // `/api` prefix. Unproxied, Vite answers it with index.html at 200, and
      // the client — which only parses a JSON content-type — hands the HTML
      // back as a string that passes a truthiness check and then has no
      // `components` on it. Dev has to forward this or it diverges from :3000.
      '/health': { target: 'http://localhost:3000', changeOrigin: true },
    },
  },
});
