import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import './industry.css';
import './app.css';
import { App } from './App.js';
import { ErrorBoundary } from './states/ErrorBoundary.js';

/**
 * Cache per query key, invalidated on the matching SSE event — so a stage
 * change arriving while the user is looking at the list animates the row rather
 * than reloading the view.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: (failureCount, error) =>
        (error as { status?: number }).status === 401 ? false : failureCount < 2,
      refetchOnWindowFocus: true,
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </ErrorBoundary>
  </StrictMode>,
);

// Offline read-only mode. The service worker caches GET responses so an
// installed PWA on a train still shows the pipeline it last saw.
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js');
  });
}
