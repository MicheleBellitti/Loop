import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangle } from 'lucide-react';
import { api, type MailboxHealth } from '../api.js';
import { Blueprint, Button } from '../components.js';

/**
 * F1 — access revoked.
 *
 * The only full-screen failure in the product, because it is the only one the
 * system cannot fix alone. It states when the data was last trustworthy and
 * whether anything was lost — the two things every failure here must answer.
 */
export function AccessRevoked({ health }: { health: MailboxHealth }) {
  const counts = useQuery({
    queryKey: ['applications', 'count'],
    queryFn: () => api.get<{ rows: unknown[] }>('/api/applications?limit=200'),
  });

  const reconnect = useMutation({
    mutationFn: () => api.post<{ url: string }>('/api/mailboxes/gmail/start'),
    onSuccess: (res) => {
      window.location.href = res.url;
    },
  });

  const lastOk = health.last_ok_at ? new Date(health.last_ok_at) : null;
  const days = lastOk ? Math.max(0, Math.floor((Date.now() - lastOk.getTime()) / 86_400_000)) : null;

  return (
    <div style={{ padding: 'calc(env(safe-area-inset-top, 0px) + 32px) 18px 40px', maxWidth: 520, margin: '0 auto' }}>
      <Blueprint style={{ padding: 'var(--space-4)', borderColor: 'var(--color-accent-400)' }}>
        <div style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center', color: 'var(--color-accent-800)' }}>
          <AlertTriangle size={20} strokeWidth={1.5} />
          <strong style={{ font: '600 18px var(--font-heading)' }}>
            Loop stopped reading{days !== null ? ` ${days} day${days === 1 ? '' : 's'} ago` : ''}
          </strong>
        </div>
      </Blueprint>

      <p className="muted-72" style={{ fontSize: 14, lineHeight: 1.6, marginTop: 'var(--space-6)' }}>
        Nothing has been lost. Your {counts.data?.rows.length ?? 0} applications and their whole history
        are intact — Loop simply has not seen anything new since{' '}
        {lastOk ? lastOk.toLocaleDateString('en-GB', { weekday: 'long' }) : 'then'}, and it will catch up
        on every missed message the moment you reconnect.
      </p>

      <div style={{ border: '1px solid var(--color-divider)', marginTop: 'var(--space-6)' }}>
        <Row label="Last successful read" value={lastOk ? lastOk.toLocaleString('en-GB', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : 'unknown'} />
        <Row label="Applications kept" value={String(counts.data?.rows.length ?? 0)} />
        <Row label="Estimated missed messages" value={health.backlog ? `~${health.backlog}` : 'unknown'} last />
      </div>

      <div style={{ display: 'grid', gap: 'var(--space-2)', marginTop: 'var(--space-6)' }}>
        <Button variant="primary" style={{ minHeight: 50 }} onClick={() => reconnect.mutate()} disabled={reconnect.isPending}>
          Reconnect Google
        </Button>
        <Button onClick={() => reconnect.mutate()}>Use a different mailbox</Button>
      </div>
    </div>
  );
}

function Row({ label, value, last = false }: { label: string; value: string; last?: boolean }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        padding: 'var(--space-3)',
        borderBottom: last ? undefined : '1px solid var(--color-divider)',
      }}
    >
      <span className="muted-65" style={{ fontSize: 13 }}>
        {label}
      </span>
      <span style={{ fontSize: 13.5, fontWeight: 500 }}>{value}</span>
    </div>
  );
}
