import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type ApplicationDetail } from '../api.js';
import { Button, Mark, Provenance, Skeleton, Tag } from '../components.js';

/**
 * The detail drawer — the same content as the mobile detail view, with the
 * actions on one row. 520px, right-anchored over a 35% scrim.
 */
export function Drawer({ id, onClose }: { id: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['application', id],
    queryFn: () => api.get<ApplicationDetail>(`/api/applications/${id}`),
  });

  const archive = useMutation({
    mutationFn: () => api.post(`/api/applications/${id}/archive`, { as: 'dormant' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['applications'] });
      onClose();
    },
  });

  return (
    <>
      <div
        className="scrim"
        style={{ background: 'color-mix(in srgb, var(--color-neutral-900) 35%, transparent)' }}
        onClick={onClose}
      />
      <aside className="drawer" role="dialog" aria-label="Application record">
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
            <div style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-6)' }}>
              <Tag tone="accent">{data.display_stage}</Tag>
              {data.channel ? <Tag>{data.channel.replace(/_/g, ' ')}</Tag> : null}
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

            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 'var(--space-2)', marginTop: 'var(--space-6)' }}>
              <Button variant="primary">Draft follow-up</Button>
              <Button
                disabled={!data.facts.posting_url}
                onClick={() => data.facts.posting_url && window.open(data.facts.posting_url, '_blank', 'noopener,noreferrer')}
              >
                Open thread
              </Button>
              <Button>Correct stage</Button>
              <Button onClick={() => archive.mutate()} disabled={archive.isPending}>
                Archive
              </Button>
            </div>
          </>
        )}
      </aside>
    </>
  );
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
