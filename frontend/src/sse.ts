import { useEffect } from 'react';
import type { QueryClient } from '@tanstack/react-query';

/**
 * The live connection.
 *
 * Four events, each invalidating exactly the query keys it affects. Nothing
 * here recomputes state — the server is authoritative, so an event is a hint to
 * refetch, never a patch applied blind.
 */
export function useLiveUpdates(queryClient: QueryClient, enabled: boolean): void {
  useEffect(() => {
    if (!enabled) return;
    const source = new EventSource('/api/stream', { withCredentials: true });

    const invalidate = (keys: string[][]) => {
      for (const key of keys) void queryClient.invalidateQueries({ queryKey: key });
    };

    source.addEventListener('application.changed', () =>
      invalidate([['today'], ['applications'], ['stats'], ['application']]),
    );
    source.addEventListener('scan.progress', (e) => {
      queryClient.setQueryData(['scan'], JSON.parse((e as MessageEvent).data));
      invalidate([['mailboxes']]);
    });
    source.addEventListener('review.added', () => invalidate([['review'], ['today']]));
    source.addEventListener('mailbox.status', () => invalidate([['mailboxes'], ['today']]));

    // EventSource reconnects on its own; this only stops an error storm from
    // filling the console on a box that is genuinely down.
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) source.close();
    };

    return () => source.close();
  }, [queryClient, enabled]);
}
