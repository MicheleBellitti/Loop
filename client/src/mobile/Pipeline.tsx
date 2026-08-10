import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type ApplicationRow } from '../api.js';
import { Skeleton } from '../components.js';

/** Groups render in this fixed order; empty groups are omitted entirely. */
const PHASE_ORDER = ['interviewing', 'screening', 'sent', 'decided'] as const;
const PHASE_LABELS: Record<string, string> = {
  interviewing: 'Interviewing',
  screening: 'Screening',
  sent: 'Sent',
  decided: 'Decided',
};

/** Pipeline — every application in one thumb-scroll, grouped by phase. */
export function Pipeline({ onOpen }: { onOpen: (id: string) => void }) {
  const [filter, setFilter] = useState<string>('all');

  const { data, isLoading } = useQuery({
    queryKey: ['applications', filter],
    queryFn: () =>
      api.get<{ rows: ApplicationRow[]; next_cursor: string | null }>(
        `/api/applications${filter === 'all' ? '' : `?phase=${filter}`}`,
      ),
  });

  if (isLoading || !data) {
    return (
      <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
        <Skeleton width="60%" height={32} />
        <Skeleton height={36} />
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} height={64} />
        ))}
      </div>
    );
  }

  const rows = data.rows;
  const groups = PHASE_ORDER.map((phase) => ({
    phase,
    label: PHASE_LABELS[phase]!,
    rows: rows.filter((r) => r.phase === phase),
  })).filter((g) => g.rows.length > 0);

  const filters: Array<[string, string]> = [
    ['all', `All ${rows.length}`],
    ['interviewing', 'Interviewing'],
    ['screening', 'Screening'],
    ['sent', 'Sent'],
    ['decided', 'Decided'],
  ];

  return (
    <div style={{ margin: '0 -18px' }}>
      <div style={{ padding: '0 18px' }}>
        <div className="eyebrow">Pipeline</div>
        <h1
          className="headline"
          style={{ fontSize: 32, marginBottom: 'var(--space-4)' }}
        >
          {rows.length} {rows.length === 1 ? 'application' : 'applications'}
        </h1>
      </div>

      <div className="filter-row" role="group" aria-label="Filter by phase">
        {filters.map(([key, label]) => (
          <button
            key={key}
            className="filter-chip"
            aria-pressed={filter === key}
            onClick={() => setFilter(key)}
          >
            {label}
          </button>
        ))}
      </div>

      {groups.map((g) => (
        <section key={g.phase}>
          <div className="group-header">
            <span>{g.label}</span>
            <span>{g.rows.length}</span>
          </div>
          {g.rows.map((r) => (
            <button
              key={r.id}
              className={`pipeline-row ${r.closed ? 'closed' : ''}`}
              onClick={() => onOpen(r.id)}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-3)' }}>
                <span className="pipeline-company">{r.company}</span>
                <span className="pipeline-stage">{r.display_stage}</span>
              </div>
              <div className="muted-70" style={{ fontSize: 13.5 }}>
                {r.role}
              </div>
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 'var(--space-2)',
                  fontSize: 11.5,
                  marginTop: 3,
                }}
                className="muted-50"
              >
                {r.channel ? <span>{channelLabel(r.channel)}</span> : null}
                {r.channel ? (
                  <span style={{ width: 1, height: 10, background: 'var(--color-divider)' }} />
                ) : null}
                <span>{r.quiet_label}</span>
                {r.flag ? (
                  <span style={{ marginLeft: 'auto', color: 'var(--color-accent-700)', fontWeight: 500 }}>
                    {r.flag}
                  </span>
                ) : null}
              </div>
            </button>
          ))}
        </section>
      ))}

      {rows.length === 0 ? (
        <p className="muted-60" style={{ padding: '0 18px', fontSize: 13.5 }}>
          Nothing in this group.
        </p>
      ) : null}
    </div>
  );
}

function channelLabel(channel: string): string {
  const labels: Record<string, string> = {
    linkedin: 'LinkedIn',
    indeed: 'Indeed',
    career_page: 'Career page',
    referral: 'Referral',
    recruiter: 'Recruiter',
    other: 'Other',
  };
  return labels[channel] ?? channel;
}
