import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type Stats as StatsData } from '../api.js';
import { Blueprint, Figure, Skeleton } from '../components.js';

/** Statistics — honest numbers. Every ratio shows its denominator. */
export function Stats() {
  const [period, setPeriod] = useState<'90d' | '12m'>('12m');
  const { data, isLoading } = useQuery({
    queryKey: ['stats', period],
    queryFn: () => api.get<StatsData>(`/api/stats?period=${period}`),
  });

  if (isLoading || !data) {
    return (
      <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
        <Skeleton width="50%" height={32} />
        <Skeleton height={40} />
        <Skeleton height={160} />
        <Skeleton height={100} />
      </div>
    );
  }

  const enoughToShow = data.ratios.some((r) => r.gate_met);

  return (
    <>
      <div className="eyebrow">Statistics</div>
      <h1 className="headline" style={{ fontSize: 32, marginBottom: 'var(--space-4)' }}>
        {enoughToShow ? (
          <>Last {period === '12m' ? '12 months' : '90 days'}</>
        ) : (
          // E3 — not enough data to be honest.
          <>
            <span style={{ display: 'block' }}>Too early</span>
            <span style={{ display: 'block' }}>to mean</span>
            <span style={{ display: 'block' }}>anything</span>
          </>
        )}
      </h1>

      <div className="seg" role="group" aria-label="Period">
        <button aria-pressed={period === '90d'} onClick={() => setPeriod('90d')}>
          90 days
        </button>
        <button aria-pressed={period === '12m'} onClick={() => setPeriod('12m')}>
          12 months
        </button>
      </div>

      <section style={{ marginTop: 'var(--space-6)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>
          Funnel
        </div>
        <div style={{ border: '1px solid var(--color-divider)' }}>
          {data.funnel.map((f) => (
            <div key={f.label} style={{ padding: 'var(--space-3)', borderBottom: '1px solid var(--color-divider)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <span className="muted-70" style={{ fontSize: 13 }}>
                  {f.label}
                </span>
                <span style={{ font: '600 17px var(--font-heading)' }}>{f.n}</span>
              </div>
              <div className="bar-track" style={{ marginTop: 6 }}>
                <div className="bar-fill" style={{ width: `${f.width}%` }} />
              </div>
            </div>
          ))}
        </div>
      </section>

      <section style={{ marginTop: 'var(--space-6)', display: 'grid', gap: 'var(--space-3)' }}>
        {data.ratios.map((r) => (
          <Figure key={r.label} label={r.label} value={r.display} note={r.note} gateMet={r.gate_met} />
        ))}
      </section>

      <section
        style={{
          marginTop: 'var(--space-4)',
          display: 'grid',
          gridTemplateColumns: '1fr 1fr',
          border: '1px solid var(--color-divider)',
        }}
      >
        <div style={{ padding: 'var(--space-4)', borderRight: '1px solid var(--color-divider)' }}>
          <div style={{ font: '600 26px var(--font-heading)' }}>{data.first_response.display}</div>
          <div className="muted-55" style={{ fontSize: 11.5 }}>
            {data.first_response.caption}
          </div>
        </div>
        <div style={{ padding: 'var(--space-4)' }}>
          <div style={{ font: '600 26px var(--font-heading)' }}>{data.ghost.gate_met ? data.ghost.display : '—'}</div>
          <div className="muted-55" style={{ fontSize: 11.5 }}>
            {data.ghost.caption}
          </div>
        </div>
      </section>

      <section style={{ marginTop: 'var(--space-6)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>
          By channel
        </div>
        <table className="table">
          <thead>
            <tr>
              <th>Channel</th>
              <th>Sent</th>
              <th>→ IV</th>
              <th>→ Offer</th>
              <th>Ghost</th>
            </tr>
          </thead>
          <tbody>
            {data.channels.map((c) => (
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
          {data.channel_note}
        </p>
      </section>

      {data.time_in_stage.length > 0 ? (
        <section style={{ marginTop: 'var(--space-6)' }}>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>
            Median time in stage
          </div>
          {data.time_in_stage.map((t) => (
            <div
              key={t.stage}
              style={{
                display: 'grid',
                gridTemplateColumns: '118px 1fr 42px',
                alignItems: 'center',
                gap: 'var(--space-2)',
                marginBottom: 6,
              }}
            >
              <span className="muted-70" style={{ fontSize: 12.5 }}>
                {t.stage}
              </span>
              <div className="bar-track" style={{ height: 6 }}>
                <div
                  style={{
                    height: '100%',
                    background: 'var(--color-accent-500)',
                    width: `${Math.min(100, (t.days / 14) * 100)}%`,
                  }}
                />
              </div>
              {/* The server formats it. `t.days` is a percentile over epoch
                  seconds and prints as 12.416666666666666 unaided. */}
              <span style={{ fontSize: 12.5, textAlign: 'right' }} className="muted-70">
                {t.gate_met ? t.display : `${t.n}/5`}
              </span>
            </div>
          ))}
        </section>
      ) : null}

      {data.compensation.domain ? (
        <section style={{ marginTop: 'var(--space-6)' }}>
          <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>
            Compensation
          </div>
          <Blueprint style={{ padding: 'var(--space-4)' }}>
            {/* Three tracks over one shared implicit scale, computed from a real
                min/max domain rather than the prototype's fixed percentages. */}
            <Track label={`Posted range`} >
              {data.compensation.posted.map((p, i) => (
                <span
                  key={i}
                  style={{
                    position: 'absolute',
                    left: `${p.from}%`,
                    width: `${Math.max(2, p.to - p.from)}%`,
                    height: 8,
                    background: 'var(--color-accent-300)',
                  }}
                />
              ))}
            </Track>
            <Track label="Your ask">
              {data.compensation.ask ? (
                <span
                  style={{
                    position: 'absolute',
                    left: `${data.compensation.ask.at}%`,
                    width: 2,
                    height: 12,
                    background: 'var(--color-text)',
                  }}
                />
              ) : null}
            </Track>
            <Track label="Offers received">
              {data.compensation.offers.map((o, i) => (
                <span
                  key={i}
                  style={{
                    position: 'absolute',
                    left: `${o.at}%`,
                    width: 2,
                    height: 12,
                    background: 'var(--color-accent)',
                  }}
                />
              ))}
            </Track>
            {data.compensation.dropped > 0 ? (
              <p className="muted-50" style={{ fontSize: 11.5, margin: 'var(--space-3) 0 0' }}>
                {data.compensation.dropped} figure{data.compensation.dropped === 1 ? '' : 's'} in another
                currency, listed separately rather than converted.
              </p>
            ) : null}
          </Blueprint>
        </section>
      ) : null}

      {!data.seasonal.gate_met ? (
        <p className="muted-50" style={{ fontSize: 12, lineHeight: 1.5, marginTop: 'var(--space-6)' }}>
          {data.seasonal.note}
        </p>
      ) : null}

      {!enoughToShow ? (
        <div
          style={{
            marginTop: 'var(--space-6)',
            background: 'var(--color-accent-100)',
            padding: 'var(--space-4)',
          }}
        >
          <div className="section-label" style={{ color: 'var(--color-accent-800)' }}>
            Unlocks at
          </div>
          <ul className="muted-70" style={{ fontSize: 12.5, margin: 'var(--space-2) 0 0', paddingLeft: 16 }}>
            <li>Ratios · 8 closed applications</li>
            <li>Time in stage · 5 stage changes</li>
            <li>Seasonal shape · 2 quarters of history</li>
          </ul>
        </div>
      ) : null}
    </>
  );
}

function Track({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 'var(--space-4)' }}>
      <div className="muted-60" style={{ fontSize: 11.5, marginBottom: 4 }}>
        {label}
      </div>
      <div style={{ position: 'relative', height: 12 }}>
        <span
          style={{
            position: 'absolute',
            inset: '5px 0 auto 0',
            height: 1,
            background: 'var(--color-divider)',
          }}
        />
        {children}
      </div>
    </div>
  );
}
