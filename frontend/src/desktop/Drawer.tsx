import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, ApiError, type ApplicationDetail } from '../api.js';
import { Button, Mark, Provenance, Skeleton, Tag, Toast } from '../components.js';
import { DraftSheet } from '../sheets/DraftSheet.js';
import { StagePicker } from '../components.js';

/**
 * The detail drawer — the same content as the mobile detail view, with the
 * actions on one row. 520px, right-anchored over a 35% scrim.
 *
 * All four actions used to be decoration. Two had no handler at all, `Open
 * thread` was wired to a posting URL that is null on most rows so it sat
 * disabled, and `Archive` fired into a mutation with no success and no failure
 * state — the drawer closed whatever happened, including when the request came
 * back 403. A control that looks pressable and does nothing is read as broken
 * software rather than as an unbuilt feature, so each one now either does the
 * thing or says why it cannot.
 */
export function Drawer({ id, onClose }: { id: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [correcting, setCorrecting] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const panel = useRef<HTMLElement>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['application', id],
    queryFn: () => api.get<ApplicationDetail>(`/api/applications/${id}`),
  });

  // Escape closes the drawer, and focus moves into it when it opens. Neither
  // was true before, so a keyboard user could tab behind the scrim into a table
  // they could not see.
  useEffect(() => {
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    panel.current?.focus();
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: ['applications'] });
    void queryClient.invalidateQueries({ queryKey: ['application', id] });
    void queryClient.invalidateQueries({ queryKey: ['today'] });
    void queryClient.invalidateQueries({ queryKey: ['stats'] });
  };

  const archive = useMutation({
    mutationFn: () => api.post(`/api/applications/${id}/archive`, { as: 'dormant' }),
    onSuccess: () => {
      refresh();
      onClose();
    },
    onError: (error) => setToast(failure(error, 'Archiving failed. Nothing was changed.')),
  });

  const correct = useMutation({
    mutationFn: (to: string) => api.post(`/api/applications/${id}/correct`, { field: 'stage', to }),
    onSuccess: () => {
      setCorrecting(false);
      setToast('Correction recorded. The agent will not overwrite it.');
      refresh();
    },
    onError: (error) => setToast(failure(error, 'That did not save. Nothing was changed.')),
  });

  return (
    <>
      <div
        className="scrim"
        style={{ background: 'color-mix(in srgb, var(--color-neutral-900) 35%, transparent)' }}
        onClick={onClose}
      />
      <aside className="drawer" role="dialog" aria-modal="true" aria-label="Application record" ref={panel} tabIndex={-1}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <span className="eyebrow">Application record</span>
          <button onClick={onClose} style={{ background: 'none', border: 0 }} className="eyebrow">
            Close ×
          </button>
        </div>

        {isLoading || !data ? (
          <div style={{ display: 'grid', gap: 'var(--space-4)', marginTop: 'var(--space-6)' }}>
            <Skeleton width="60%" height={32} />
            <Skeleton height={100} />
            <Skeleton height={200} />
          </div>
        ) : (
          <>
            <h2 className="headline" style={{ fontSize: 32, marginTop: 'var(--space-3)' }}>
              {data.company}
            </h2>
            <p className="muted-72" style={{ fontSize: 14.5, margin: '2px 0 var(--space-3)' }}>
              {data.role}
            </p>
            <div style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-6)', flexWrap: 'wrap' }}>
              <Tag tone="accent">{data.display_stage}</Tag>
              {data.channel ? <Tag>{data.channel.replace(/_/g, ' ')}</Tag> : null}
              <Tag>{activityLabel(data)}</Tag>
            </div>

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: '1fr 1fr',
                border: '1px solid var(--color-divider)',
                marginBottom: 'var(--space-6)',
              }}
            >
              <Fact label="Applied" value={data.facts.applied ? new Date(data.facts.applied).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : '—'} border />
              <Fact label="ATS" value={data.facts.ats ?? '—'} />
              <Fact label="Location" value={data.facts.location ?? '—'} border top />
              <Fact label="Posted range" value={data.facts.posted_range ? `${money(data.facts.posted_range.min_minor, data.facts.posted_range.currency)}+` : 'not posted'} top />
            </div>

            <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>
              Event log
            </div>
            {data.events.map((e, i) => (
              <div className="event" key={e.id}>
                <div className="event-when">
                  {new Date(e.when).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}
                </div>
                <div className="event-rail">
                  <Mark />
                  {i < data.events.length - 1 ? <span className="event-rail-line" /> : null}
                </div>
                <div>
                  <div style={{ fontSize: 14.5, fontWeight: 500 }}>{e.what}</div>
                  {e.detail ? (
                    <div className="muted-65" style={{ fontSize: 12.5 }}>
                      {e.detail}
                    </div>
                  ) : null}
                  <div style={{ marginTop: 5 }}>
                    <Provenance source={e.source} conf={e.conf} />
                  </div>
                </div>
              </div>
            ))}

            <div className="drawer-actions">
              <Button variant="primary" onClick={() => setDrafting(true)}>
                Draft follow-up
              </Button>
              <Button
                disabled={!data.facts.posting_url}
                title={data.facts.posting_url ?? 'No posting URL was ever seen for this application'}
                onClick={() => data.facts.posting_url && window.open(data.facts.posting_url, '_blank', 'noopener,noreferrer')}
              >
                Open posting
              </Button>
              <Button aria-expanded={correcting} onClick={() => setCorrecting((v) => !v)}>
                Correct stage
              </Button>
              <Button onClick={() => archive.mutate()} disabled={archive.isPending}>
                {archive.isPending ? 'Archiving…' : 'Archive'}
              </Button>
            </div>

            {correcting ? (
              <StagePicker
                busy={correct.isPending}
                current={data.stage}
                onPick={(key) => correct.mutate(key)}
                note="Correcting the stage writes a human_corrected event at confidence 1.0 — the agent will not overwrite it."
              />
            ) : null}

            {drafting ? <DraftSheet applicationId={id} onClose={() => setDrafting(false)} /> : null}
          </>
        )}
      </aside>
      {toast ? <Toast message={toast} onDismiss={() => setToast(null)} /> : null}
    </>
  );
}

/**
 * §13: the client reads `error.code`, never `error.message`. Two of these are
 * worth their own sentence because the fix is different — signing in again, or
 * waiting for the pipeline.
 */
export function failure(error: unknown, fallback: string): string {
  const code = error instanceof ApiError ? error.code : 'unknown';
  if (code === 'csrf' || code === 'unauthenticated') return 'Your session expired. Reload and sign in again.';
  if (code === 'not_found') return 'That application is no longer there. Nothing was changed.';
  return fallback;
}

function activityLabel(row: { activity: string; days_quiet: number | null }): string {
  if (row.activity === 'closed') return 'closed';
  if (row.activity === 'stale') return `quiet ${row.days_quiet ?? 0} days`;
  return 'in progress';
}

function Fact({ label, value, border = false, top = false }: { label: string; value: string; border?: boolean; top?: boolean }) {
  return (
    <div
      style={{
        padding: 'var(--space-3)',
        borderRight: border ? '1px solid var(--color-divider)' : undefined,
        borderTop: top ? '1px solid var(--color-divider)' : undefined,
      }}
    >
      <div className="muted-50" style={{ font: '600 10px var(--font-heading)', letterSpacing: '.11em', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div style={{ fontSize: 14, marginTop: 2 }}>{value}</div>
    </div>
  );
}

function money(minor: string | number, currency: string): string {
  return new Intl.NumberFormat('en-GB', { style: 'currency', currency, maximumFractionDigits: 0 }).format(
    Number(minor) / 100,
  );
}
