import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type ApplicationRow, type MailboxHealth, type Stats, type Today } from '../api.js';
import { Button, Figure, Skeleton, Toast } from '../components.js';
import { Drawer } from './Drawer.js';
import { ComponentStatus } from '../states/CatchingUp.js';
import { ReviewQueue } from '../sheets/ReviewQueue.js';

/**
 * The desktop dashboard: dense table, bulk work, analytics rail.
 *
 * 1440px reference width; the layout is fluid with a fixed 400px right rail
 * that collapses below the table under ~1100px.
 */

type Sort = 'last_signal' | 'stage_depth' | 'company';

/**
 * The four things the top bar can show.
 *
 * They were drawn in the prototype and shipped as static markup: four buttons,
 * `aria-current` hard-coded on the first, no handler on any of them. The bar
 * looked like navigation and was decoration, which is worse than not having it
 * — the user reads it as broken software rather than as an unbuilt feature.
 */
type View = 'pipeline' | 'statistics' | 'review' | 'settings';

export function Dashboard() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<View>('pipeline');
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
          {(
            [
              ['pipeline', 'Pipeline'],
              ['statistics', 'Statistics'],
              ['review', `Review${today.data?.review_count ? ` · ${today.data.review_count}` : ''}`],
              ['settings', 'Settings'],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              className="nav-item"
              aria-current={view === key ? 'page' : undefined}
              onClick={() => {
                setView(key);
                setOpenId(null);
              }}
            >
              {label}
            </button>
          ))}
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

      {view === 'pipeline' ? (
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

          {deep.data?.components ? (
            <section>
              <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>Extraction</div>
              <ComponentStatus components={deep.data.components} />
            </section>
          ) : null}
        </aside>
      </div>
      ) : null}

      {view === 'statistics' ? (
        <StatisticsView
          stats={stats.data}
          period={period}
          onPeriod={setPeriod}
          components={deep.data?.components}
        />
      ) : null}

      {view === 'review' ? (
        <div className="desk-single">
          <ReviewQueue onClose={() => setView('pipeline')} />
        </div>
      ) : null}

      {view === 'settings' ? <SettingsView health={health.data} /> : null}

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

/**
 * Statistics at full width.
 *
 * The same figures the pipeline rail carries, but given the room to be read
 * rather than glanced at — and with the two panels the rail has no space for:
 * time in stage, and the compensation spread. Every ratio keeps its
 * denominator, because a ratio without one is a bug, not a smaller feature.
 */
function StatisticsView({
  stats,
  period,
  onPeriod,
  components,
}: {
  stats: Stats | undefined;
  period: '90d' | '12m';
  onPeriod: (p: '90d' | '12m') => void;
  components?: { template_rules: string; calendar_detection: string; local_model: string };
}) {
  if (!stats) return <div className="desk-single"><Skeleton height={320} /></div>;

  return (
    <div className="desk-single">
      <div className="seg" role="group" aria-label="Period" style={{ maxWidth: 260, marginBottom: 'var(--space-6)' }}>
        <button aria-pressed={period === '90d'} onClick={() => onPeriod('90d')}>90 days</button>
        <button aria-pressed={period === '12m'} onClick={() => onPeriod('12m')}>12 months</button>
      </div>

      <div className="stats-grid">
        <section>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>Funnel</div>
          <div style={{ border: '1px solid var(--color-divider)' }}>
            {stats.funnel.map((f) => (
              <div key={f.label} style={{ padding: 'var(--space-3)', borderBottom: '1px solid var(--color-divider)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span className="muted-70" style={{ fontSize: 13 }}>{f.label}</span>
                  <span style={{ font: '600 17px var(--font-heading)' }}>{f.n}</span>
                </div>
                <div className="bar-track" style={{ height: 8, marginTop: 6 }}>
                  <div className="bar-fill" style={{ width: `${f.width}%` }} />
                </div>
              </div>
            ))}
          </div>
        </section>

        <section style={{ display: 'grid', gap: 'var(--space-4)', alignContent: 'start' }}>
          {stats.ratios.map((r) => (
            <Figure key={r.label} label={r.label} value={r.display} note={r.note} gateMet={r.gate_met} />
          ))}
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-4)' }}>
            <div>
              <div style={{ font: '600 26px var(--font-heading)' }}>{stats.first_response.display}</div>
              <div className="muted-55" style={{ fontSize: 12 }}>{stats.first_response.caption}</div>
            </div>
            <div>
              <div style={{ font: '600 26px var(--font-heading)' }}>{stats.ghost.display}</div>
              <div className="muted-55" style={{ fontSize: 12 }}>{stats.ghost.caption}</div>
            </div>
          </div>
        </section>

        <section>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>By channel</div>
          <table className="table">
            <thead>
              <tr><th>Channel</th><th>Sent</th><th>→ IV</th><th>→ Offer</th><th>Ghost</th></tr>
            </thead>
            <tbody>
              {stats.channels.map((c) => (
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
          <p className="muted-50" style={{ fontSize: 11.5, marginTop: 'var(--space-2)' }}>{stats.channel_note}</p>
        </section>

        <section>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>Median time in stage</div>
          {stats.time_in_stage.length === 0 ? (
            <p className="muted-55" style={{ fontSize: 12.5 }}>No stage changes recorded yet.</p>
          ) : (
            stats.time_in_stage.map((t) => (
              <div
                key={t.stage}
                style={{ display: 'grid', gridTemplateColumns: '128px 1fr 52px', gap: 'var(--space-3)', alignItems: 'center', marginBottom: 6 }}
              >
                <span className="muted-70" style={{ fontSize: 12.5 }}>{t.stage.replace(/_/g, ' ')}</span>
                <div className="bar-track" style={{ height: 6 }}>
                  <div
                    className="bar-fill"
                    style={{
                      width: `${Math.min(100, (t.days / Math.max(...stats.time_in_stage.map((x) => x.days), 1)) * 100)}%`,
                      background: 'var(--color-accent-500)',
                    }}
                  />
                </div>
                <span style={{ fontSize: 12.5, textAlign: 'right' }} className={t.gate_met ? undefined : 'muted-50'}>
                  {t.gate_met ? `${t.days} d` : `${t.n}/5`}
                </span>
              </div>
            ))
          )}
        </section>
      </div>

      <p className="muted-50" style={{ fontSize: 11.5, marginTop: 'var(--space-6)' }}>{stats.seasonal.note}</p>

      {components ? (
        <section style={{ marginTop: 'var(--space-6)', maxWidth: 420 }}>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>Extraction</div>
          <ComponentStatus components={components} />
        </section>
      ) : null}
    </div>
  );
}

/**
 * Settings.
 *
 * Deliberately small: what is connected, how to get your data out, and how to
 * destroy it. The two GDPR endpoints are the ones §15 says must be reachable
 * "by construction, not by a support inbox", so they are buttons rather than
 * documentation.
 */
function SettingsView({ health }: { health: MailboxHealth | undefined }) {
  const reconnect = useMutation({
    mutationFn: () => api.post<{ url: string }>('/api/mailboxes/gmail/start'),
    onSuccess: (res) => {
      window.location.href = res.url;
    },
  });

  return (
    <div className="desk-single" style={{ maxWidth: 720 }}>
      <section style={{ marginBottom: 'var(--space-8)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Mailboxes</div>
        <div style={{ border: '1px solid var(--color-divider)' }}>
          {(health?.providers ?? []).map((p) => (
            <div
              key={p.id}
              style={{ padding: 'var(--space-4)', borderBottom: '1px solid var(--color-divider)', display: 'flex', justifyContent: 'space-between' }}
            >
              <span>{p.provider === 'gmail' ? 'Gmail' : 'Google Calendar'}<span className="muted-65"> · {p.address}</span></span>
              <span className={p.status === 'ok' ? 'emphasis' : 'muted-65'}>{p.status}</span>
            </div>
          ))}
          {!health?.providers.length ? (
            <div style={{ padding: 'var(--space-4)' }} className="muted-65">No mailbox connected.</div>
          ) : null}
        </div>
        <div style={{ marginTop: 'var(--space-3)' }}>
          <Button onClick={() => reconnect.mutate()} disabled={reconnect.isPending}>
            {health?.connected ? 'Reconnect Google' : 'Connect a mailbox'}
          </Button>
        </div>
      </section>

      <section style={{ marginBottom: 'var(--space-8)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Your data</div>
        <p className="muted-70" style={{ fontSize: 13, marginBottom: 'var(--space-3)' }}>
          The complete event log and every application, machine-readable, no rate limit. This is
          Article 15 and Article 20 satisfied by an endpoint rather than by a request.
        </p>
        <div style={{ display: 'flex', gap: 'var(--space-3)' }}>
          <a className="btn" href="/api/export?format=json">Export JSON</a>
          <a className="btn" href="/api/export?format=csv">Export CSV</a>
        </div>
      </section>

      <section>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Delete everything</div>
        <p className="muted-70" style={{ fontSize: 13, marginBottom: 'var(--space-3)' }}>
          A real cascade: applications, the event log, the queues, the projections, the vector
          index, push subscriptions, and the OAuth grant at Google. It returns a receipt id and it
          cannot be undone.
        </p>
        <a className="btn" href="/settings/delete">Delete my account…</a>
      </section>
    </div>
  );
}
