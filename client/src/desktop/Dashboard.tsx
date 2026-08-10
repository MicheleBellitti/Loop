import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type ApplicationRow, type MailboxHealth, type Stats, type Today } from '../api.js';
import { Figure, Skeleton, Toast } from '../components.js';
import { Drawer } from './Drawer.js';
import { ComponentStatus } from '../states/CatchingUp.js';

/**
 * The desktop dashboard: dense table, bulk work, analytics rail.
 *
 * 1440px reference width; the layout is fluid with a fixed 400px right rail
 * that collapses below the table under ~1100px.
 */

type Sort = 'last_signal' | 'stage_depth' | 'company';

export function Dashboard() {
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState('all');
  const [sort, setSort] = useState<Sort>('last_signal');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [openId, setOpenId] = useState<string | null>(null);
  const [period, setPeriod] = useState<'90d' | '12m'>('12m');
  const [toast, setToast] = useState<{ message: string; undo?: () => void } | null>(null);

  const today = useQuery({ queryKey: ['today'], queryFn: () => api.get<Today>('/api/today') });
  const health = useQuery({ queryKey: ['mailboxes'], queryFn: () => api.get<MailboxHealth>('/api/mailboxes') });
  const deep = useQuery({
    queryKey: ['health-deep'],
    queryFn: () => api.get<{ components: { template_rules: string; calendar_detection: string; local_model: string } }>('/health/deep'),
    refetchInterval: 60_000,
  });
  const apps = useQuery({
    queryKey: ['applications', phase, sort],
    queryFn: () =>
      api.get<{ rows: ApplicationRow[] }>(
        `/api/applications?sort=${sort}${phase === 'all' ? '' : `&phase=${phase}`}&limit=200`,
      ),
  });
  const stats = useQuery({ queryKey: ['stats', period], queryFn: () => api.get<Stats>(`/api/stats?period=${period}`) });

  const archive = useMutation({
    mutationFn: (ids: string[]) => api.post('/api/applications/archive', { ids, as: 'dormant' }),
    onMutate: (ids) => {
      // Optimistic, with an undo — the prototypes only imply this state.
      const previous = queryClient.getQueryData<{ rows: ApplicationRow[] }>(['applications', phase, sort]);
      queryClient.setQueryData<{ rows: ApplicationRow[] }>(['applications', phase, sort], (old) =>
        old ? { rows: old.rows.filter((r) => !ids.includes(r.id)) } : old,
      );
      return { previous };
    },
    onError: (_e, _ids, context) => {
      if (context?.previous) queryClient.setQueryData(['applications', phase, sort], context.previous);
      setToast({ message: 'Archiving failed. Nothing was changed.' });
    },
    onSuccess: () => {
      setSelected(new Set());
      setToast({ message: 'Archived as dormant. They stay in your statistics as ghosted.' });
      void queryClient.invalidateQueries({ queryKey: ['today'] });
    },
  });

  const rows = apps.data?.rows ?? [];
  const counters = today.data?.counters;
  const kpis = useMemo(
    () => [
      { n: counters?.live ?? 0, label: 'live applications' },
      { n: counters?.interviewing ?? 0, label: 'interviewing' },
      { n: counters?.offer ?? 0, label: 'open offer', tinted: true },
      { n: stats.data?.ratios[0]?.gate_met ? stats.data.ratios[0].display : '—', label: 'application → interview' },
      { n: stats.data?.first_response.display ?? '—', label: 'median first reply' },
      { n: stats.data?.ghost.gate_met ? stats.data.ghost.display : '—', label: 'ghost rate' },
    ],
    [counters, stats.data],
  );

  const toggle = (id: string): void => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div style={{ minHeight: '100%' }}>
      <header className="topbar">
        <span style={{ font: '600 18px var(--font-heading)', letterSpacing: '.03em', textTransform: 'uppercase' }}>
          Loop
        </span>
        <nav style={{ display: 'flex', gap: 'var(--space-6)' }}>
          <button className="nav-item" aria-current="page">Pipeline</button>
          <button className="nav-item">Statistics</button>
          <button className="nav-item">Review{today.data?.review_count ? ` · ${today.data.review_count}` : ''}</button>
          <button className="nav-item">Settings</button>
        </nav>
        {/* This health string is load-bearing: a silent connector is
            indistinguishable from a quiet job market. */}
        <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 'var(--space-2)', fontSize: 12.5 }}>
          <span
            style={{
              width: 7,
              height: 7,
              background: health.data?.connected ? 'var(--color-accent)' : 'var(--color-neutral-500)',
            }}
          />
          <span className="muted-65">{healthLine(health.data)}</span>
        </div>
      </header>

      <div className="kpi-strip">
        {kpis.map((k) => (
          <div key={k.label} className={k.tinted ? 'kpi-offer' : undefined}>
            <div className="kpi-n">{k.n}</div>
            <div className="kpi-label">{k.label}</div>
          </div>
        ))}
      </div>

      <div className="desk-layout">
        <main>
          <div style={{ display: 'flex', gap: 'var(--space-4)', marginBottom: 'var(--space-4)', flexWrap: 'wrap' }}>
            <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
              {['all', 'interviewing', 'screening', 'sent', 'decided'].map((p) => (
                <button key={p} className="filter-chip" aria-pressed={phase === p} onClick={() => setPhase(p)}>
                  {p}
                </button>
              ))}
            </div>
            <div style={{ display: 'flex', gap: 'var(--space-2)', marginLeft: 'auto', alignItems: 'center' }}>
              <span className="eyebrow">Sort</span>
              {(
                [
                  ['last_signal', 'Last signal'],
                  ['stage_depth', 'Stage depth'],
                  ['company', 'Company'],
                ] as Array<[Sort, string]>
              ).map(([key, label]) => (
                <button key={key} className="filter-chip" aria-pressed={sort === key} onClick={() => setSort(key)}>
                  {label}
                </button>
              ))}
            </div>
          </div>

          {selected.size > 0 ? (
            <div className="bulk-bar">
              <strong style={{ font: '600 14px var(--font-heading)' }}>{selected.size} selected</strong>
              <button className="filter-chip" onClick={() => archive.mutate([...selected])}>
                Archive as dormant
              </button>
              <button className="filter-chip" disabled>
                Set stage…
              </button>
              <a className="filter-chip" href="/api/export?format=csv" style={{ display: 'inline-flex', alignItems: 'center', textDecoration: 'none' }}>
                Export CSV
              </a>
              <button
                className="filter-chip"
                style={{ marginLeft: 'auto', border: 0 }}
                onClick={() => setSelected(new Set())}
              >
                Clear
              </button>
            </div>
          ) : null}

          {apps.isLoading ? (
            <div style={{ display: 'grid', gap: 6 }}>
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <Skeleton key={i} height={38} />
              ))}
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 34 }} />
                  <th>Company</th>
                  <th>Role</th>
                  <th>Stage</th>
                  <th>Channel</th>
                  <th style={{ textAlign: 'right' }}>Applied</th>
                  <th style={{ textAlign: 'right' }}>Last signal</th>
                  <th>Flag</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className={r.closed ? 'closed' : ''} style={{ cursor: 'pointer' }}>
                    <td onClick={(e) => e.stopPropagation()}>
                      <button
                        className="checkbox"
                        role="checkbox"
                        aria-checked={selected.has(r.id)}
                        aria-label={`Select ${r.company}`}
                        onClick={() => toggle(r.id)}
                      />
                    </td>
                    <td onClick={() => setOpenId(r.id)} style={{ fontWeight: 500 }}>
                      {r.company}
                    </td>
                    <td onClick={() => setOpenId(r.id)} className="muted-70">
                      {r.role}
                    </td>
                    <td onClick={() => setOpenId(r.id)}>{r.display_stage}</td>
                    <td onClick={() => setOpenId(r.id)} className="muted-65">
                      {r.channel?.replace(/_/g, ' ') ?? '—'}
                    </td>
                    <td onClick={() => setOpenId(r.id)} style={{ textAlign: 'right' }} className="muted-65">
                      {r.applied_at ? new Date(r.applied_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) : '—'}
                    </td>
                    <td
                      onClick={() => setOpenId(r.id)}
                      style={{ textAlign: 'right' }}
                      className={(r.days_quiet ?? 0) > 13 ? 'stale' : 'muted-65'}
                    >
                      {r.days_quiet === null ? '—' : `${r.days_quiet} d`}
                    </td>
                    <td onClick={() => setOpenId(r.id)} style={{ color: 'var(--color-accent-700)', fontSize: 12 }}>
                      {r.flag}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <p className="muted-50" style={{ fontSize: 11.5, marginTop: 'var(--space-3)' }}>
            Rows are derived, not typed. Clicking one opens its event log with the evidence behind every
            stage change.
          </p>
        </main>

        <aside style={{ display: 'grid', gap: 'var(--space-6)' }}>
          <div className="seg" role="group" aria-label="Period">
            <button aria-pressed={period === '90d'} onClick={() => setPeriod('90d')}>90 days</button>
            <button aria-pressed={period === '12m'} onClick={() => setPeriod('12m')}>12 months</button>
          </div>

          {stats.data ? (
            <>
              <section>
                <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>Funnel</div>
                <div style={{ border: '1px solid var(--color-divider)' }}>
                  {stats.data.funnel.map((f) => (
                    <div key={f.label} style={{ padding: 'var(--space-3)', borderBottom: '1px solid var(--color-divider)' }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                        <span className="muted-70" style={{ fontSize: 12.5 }}>{f.label}</span>
                        <span style={{ font: '600 15px var(--font-heading)' }}>{f.n}</span>
                      </div>
                      <div className="bar-track" style={{ height: 9, marginTop: 5 }}>
                        <div className="bar-fill" style={{ width: `${f.width}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
                {stats.data.ratios.map((r) => (
                  <Figure key={r.label} label={r.label} value={r.display} note={r.note} gateMet={r.gate_met} />
                ))}
              </div>

              <section>
                <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>By channel</div>
                <table className="table">
                  <thead>
                    <tr><th>Channel</th><th>Sent</th><th>→ IV</th><th>→ Off</th><th>Ghost</th></tr>
                  </thead>
                  <tbody>
                    {stats.data.channels.map((c) => (
                      <tr key={c.name}>
                        <td>{c.name.replace(/_/g, ' ')}</td>
                        <td>{c.sent}</td>
                        <td className="emphasis">{c.iv}</td>
                        <td>{c.of}</td>
                        <td>{c.ghost}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="muted-50" style={{ fontSize: 11.5, marginTop: 'var(--space-2)' }}>
                  {stats.data.channel_note}
                </p>
              </section>
            </>
          ) : (
            <Skeleton height={200} />
          )}

          {deep.data ? (
            <section>
              <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>Extraction</div>
              <ComponentStatus components={deep.data.components} />
            </section>
          ) : null}
        </aside>
      </div>

      {openId ? <Drawer id={openId} onClose={() => setOpenId(null)} /> : null}
      {toast ? <Toast message={toast.message} onDismiss={() => setToast(null)} /> : null}
    </div>
  );
}

function healthLine(health: MailboxHealth | undefined): string {
  if (!health) return 'checking…';
  if (!health.connected) return 'No mailbox connected';
  const providers = health.providers.map((p) => (p.provider === 'gmail' ? 'Gmail' : 'Calendar')).join(' + ');
  const minutes = health.minutes_since_read;
  const read = minutes === null ? 'never read' : minutes < 1 ? 'last read just now' : `last read ${minutes} min ago`;
  return `${providers} connected · ${read} · ${health.placed_today} messages placed today`;
}
