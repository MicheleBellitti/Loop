import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type ActivityFilter,
  type ApplicationList,
  type ApplicationRow,
  type MailboxHealth,
  type Stats,
  type Today,
} from '../api.js';
import { Button, Figure, Skeleton, StagePicker, Toast } from '../components.js';
import { Drawer, failure } from './Drawer.js';
import { ComponentStatus } from '../states/CatchingUp.js';
import { ReviewQueue } from '../sheets/ReviewQueue.js';
import { StatisticsView } from './Statistics.js';
import { ProfileMenu, SettingsView } from './Settings.js';

/**
 * The desktop dashboard: dense table, bulk work, analytics rail.
 *
 * 1440px reference width; the layout is fluid with a fixed 400px right rail
 * that collapses below the table under ~1100px.
 */

type Sort = 'last_signal' | 'stage_depth' | 'company';

/**
 * The three things the top bar can show.
 *
 * They were drawn in the prototype and shipped as static markup: four buttons,
 * `aria-current` hard-coded on the first, no handler on any of them. The bar
 * looked like navigation and was decoration, which is worse than not having it
 * — the user reads it as broken software rather than as an unbuilt feature.
 *
 * Settings left the bar afterwards: it is not a section of the product, it is
 * the account behind it, and it now hangs off the avatar where every other
 * application of this shape puts it.
 */
type View = 'pipeline' | 'statistics' | 'review' | 'settings';

/** The board's default is what is happening, not everything that ever did. */
const ACTIVITY_TABS: Array<[ActivityFilter, string]> = [
  ['open', 'In progress'],
  ['active', 'Moving'],
  ['stale', 'Quiet'],
  ['closed', 'History'],
  ['all', 'Everything'],
];

export function Dashboard() {
  const queryClient = useQueryClient();
  const [view, setView] = useState<View>('pipeline');
  const [phase, setPhase] = useState('all');
  const [activity, setActivity] = useState<ActivityFilter>('open');
  const [sort, setSort] = useState<Sort>('last_signal');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [staging, setStaging] = useState(false);
  const [openId, setOpenId] = useState<string | null>(null);
  const [period, setPeriod] = useState<'90d' | '12m'>('12m');
  const [toast, setToast] = useState<{ message: string; undo?: () => void } | null>(null);

  const me = useQuery({
    queryKey: ['me'],
    queryFn: () => api.get<{ email: string }>('/api/me'),
    staleTime: Infinity,
  });
  const today = useQuery({ queryKey: ['today'], queryFn: () => api.get<Today>('/api/today') });
  const health = useQuery({ queryKey: ['mailboxes'], queryFn: () => api.get<MailboxHealth>('/api/mailboxes') });
  const deep = useQuery({
    queryKey: ['health-deep'],
    queryFn: () => api.get<{ components: { template_rules: string; calendar_detection: string; local_model: string } }>('/health/deep'),
    refetchInterval: 60_000,
  });
  const listKey = ['applications', phase, activity, sort] as const;
  const apps = useQuery({
    queryKey: listKey,
    queryFn: () =>
      api.get<ApplicationList>(
        `/api/applications?sort=${sort}&activity=${activity}${phase === 'all' ? '' : `&phase=${phase}`}&limit=200`,
      ),
  });
  const stats = useQuery({ queryKey: ['stats', period], queryFn: () => api.get<Stats>(`/api/stats?period=${period}`) });

  const refresh = (): void => {
    void queryClient.invalidateQueries({ queryKey: ['applications'] });
    void queryClient.invalidateQueries({ queryKey: ['today'] });
    void queryClient.invalidateQueries({ queryKey: ['stats'] });
  };

  const archive = useMutation({
    mutationFn: (ids: string[]) => api.post('/api/applications/archive', { ids, as: 'dormant' }),
    onMutate: (ids) => {
      // Optimistic, with an undo — the prototypes only imply this state.
      const previous = queryClient.getQueryData<ApplicationList>(listKey);
      queryClient.setQueryData<ApplicationList>(listKey, (old) =>
        old ? { ...old, rows: old.rows.filter((r) => !ids.includes(r.id)) } : old,
      );
      return { previous };
    },
    onError: (error, _ids, context) => {
      if (context?.previous) queryClient.setQueryData(listKey, context.previous);
      setToast({ message: failure(error, 'Archiving failed. Nothing was changed.') });
    },
    onSuccess: (_data, ids) => {
      setSelected(new Set());
      setToast({
        message: `${ids.length} archived as dormant. They stay in your statistics as ghosted.`,
      });
      refresh();
    },
  });

  /**
   * Bulk stage correction. "Set stage…" sat permanently disabled, which is the
   * one thing a bulk bar is for — the per-application route already accepts the
   * correction, so this is the same call once per selected row.
   */
  const setStage = useMutation({
    mutationFn: async (to: string) => {
      const ids = [...selected];
      const results = await Promise.allSettled(
        ids.map((id) => api.post(`/api/applications/${id}/correct`, { field: 'stage', to })),
      );
      const failed = results.filter((r) => r.status === 'rejected');
      // Reported rather than swallowed: a partial failure that looks like a
      // success is how a pipeline quietly stops matching your mailbox.
      if (failed.length) throw (failed[0] as PromiseRejectedResult).reason;
      return ids.length;
    },
    onSuccess: (n, to) => {
      setStaging(false);
      setSelected(new Set());
      setToast({ message: `${n} moved to ${to.replace(/_/g, ' ')}. Recorded as your correction.` });
      refresh();
    },
    onError: (error) => setToast({ message: failure(error, 'That did not save. Nothing was changed.') }),
  });

  const rows = apps.data?.rows ?? [];
  const counts = apps.data?.counts;
  const counters = today.data?.counters;
  const kpis = useMemo(
    () => [
      { n: counters?.live ?? 0, label: 'in progress' },
      { n: counters?.quiet ?? 0, label: 'quiet, worth chasing' },
      { n: counters?.interviewing ?? 0, label: 'interviewing' },
      { n: counters?.offer ?? 0, label: 'open offer', tinted: true },
      {
        n: stats.data?.ratios[0]?.gate_met ? stats.data.ratios[0].display : '—',
        label: 'application → interview',
        // A figure withheld by a gate says which gate, here as well as on the
        // statistics page. An unexplained em dash reads as a broken number.
        note: stats.data?.ratios[0]?.gate_met ? stats.data.ratios[0].note : stats.data?.ratios[0]?.note,
      },
      {
        n: stats.data?.ghost.gate_met ? stats.data.ghost.display : '—',
        label: 'ghost rate',
        note: stats.data?.ghost.note,
      },
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

  const allShown = rows.length > 0 && rows.every((r) => selected.has(r.id));

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
        <ProfileMenu
          email={me.data?.email}
          current={view === 'settings'}
          onSettings={() => {
            setView('settings');
            setOpenId(null);
          }}
        />
      </header>

      {/* Not above the account page: the strip is the pipeline's summary, and
          settings is not one of the pipeline's screens. */}
      {view === 'settings' ? null : (
      <div className="kpi-strip">
        {kpis.map((k) => (
          <div key={k.label} className={k.tinted ? 'kpi-offer' : undefined}>
            <div className="kpi-n">{k.n}</div>
            <div className="kpi-label">{k.label}</div>
            {k.note ? <div className="kpi-note">{k.note}</div> : null}
          </div>
        ))}
      </div>
      )}

      {view === 'pipeline' ? (
      <div className="desk-layout">
        <main>
          {/* What is happening, and history behind a second click. A board that
              opens on twelve months of dead processes is a board you have to
              read past before you can work. */}
          <div style={{ display: 'flex', gap: 'var(--space-2)', marginBottom: 'var(--space-3)', flexWrap: 'wrap' }}>
            {ACTIVITY_TABS.map(([key, label]) => (
              <button
                key={key}
                className="filter-chip"
                aria-pressed={activity === key}
                onClick={() => {
                  setActivity(key);
                  setSelected(new Set());
                }}
              >
                {label}
                {counts ? <span className="chip-count">{counts[key] ?? 0}</span> : null}
              </button>
            ))}
          </div>

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
              <button
                className="filter-chip"
                disabled={archive.isPending}
                onClick={() => archive.mutate([...selected])}
              >
                {archive.isPending ? 'Archiving…' : 'Archive as dormant'}
              </button>
              <button
                className="filter-chip"
                aria-expanded={staging}
                disabled={setStage.isPending}
                onClick={() => setStaging((v) => !v)}
              >
                {setStage.isPending ? 'Saving…' : 'Set stage…'}
              </button>
              {/* Exports the selection, not the whole account — the button used
                  to link at `/api/export` flat and hand you everything while
                  sitting inside a bar that says "3 selected". */}
              <a
                className="filter-chip"
                href={`/api/export?format=csv&ids=${[...selected].join(',')}`}
                style={{ display: 'inline-flex', alignItems: 'center', textDecoration: 'none' }}
              >
                Export {selected.size} as CSV
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

          {staging && selected.size > 0 ? (
            <StagePicker
              busy={setStage.isPending}
              onPick={(key) => setStage.mutate(key)}
              note={`Applies to ${selected.size} application${selected.size === 1 ? '' : 's'}, one human_corrected event each.`}
            />
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
                  <th style={{ width: 34 }}>
                    <button
                      className="checkbox"
                      role="checkbox"
                      aria-checked={allShown}
                      aria-label={allShown ? 'Clear selection' : 'Select every row shown'}
                      disabled={rows.length === 0}
                      onClick={() => setSelected(allShown ? new Set() : new Set(rows.map((r) => r.id)))}
                    />
                  </th>
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
                  <tr
                    key={r.id}
                    className={`${r.closed ? 'closed' : ''} ${selected.has(r.id) ? 'row-selected' : ''}`}
                    style={{ cursor: 'pointer' }}
                  >
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
                    <td onClick={() => setOpenId(r.id)}>
                      {r.display_stage}
                      {/* Why this row is where it is, in one word. Without it a
                          history tab is a list of stages with no endings. */}
                      {r.activity === 'closed' && !r.closed ? (
                        <span className="row-tag" title="No signal for long enough to call it over">
                          silent
                        </span>
                      ) : null}
                      {r.activity === 'stale' ? <span className="row-tag">quiet</span> : null}
                      {r.next_interview_at ? (
                        <span className="row-tag row-tag-accent">
                          {new Date(r.next_interview_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}
                        </span>
                      ) : null}
                    </td>
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

          {!apps.isLoading && rows.length === 0 ? (
            <p className="muted-60" style={{ fontSize: 13.5, padding: 'var(--space-6) 0' }}>
              {activity === 'open'
                ? 'Nothing is in progress. Everything you have sent is either decided or has been silent long enough to count as closed — the History tab has them.'
                : 'Nothing in this group.'}
            </p>
          ) : null}

          <p className="muted-50" style={{ fontSize: 11.5, marginTop: 'var(--space-3)' }}>
            Rows are derived, not typed. Clicking one opens its event log with the evidence behind every
            stage change. {counts ? `${counts.open} in progress · ${counts.closed} closed.` : ''}
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

      {view === 'settings' ? <SettingsView health={health.data} email={me.data?.email} /> : null}

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
