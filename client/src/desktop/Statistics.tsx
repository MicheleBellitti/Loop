import type { Stats } from '../api.js';
import { Blueprint, Figure, Skeleton } from '../components.js';
import { ComponentStatus } from '../states/CatchingUp.js';

/**
 * Statistics at full width.
 *
 * The same figures the pipeline rail carries, but given the room to be read
 * rather than glanced at — and with the panels the rail has no space for: how
 * the volume moved month by month, how applications ended, time in stage, and
 * the compensation spread, which the previous version computed on the server
 * and then never rendered here at all.
 *
 * Every ratio keeps its denominator, because a ratio without one is a bug, not
 * a smaller feature. Every figure that a gate withholds says which gate.
 *
 * On the charts. The Industry palette is a desaturated blue-grey by design, so
 * series identity comes from lightness along one ramp rather than from hue —
 * which is also the honest encoding here, since the three monthly series are
 * nested (every interview is a reply, every reply an application) and the
 * outcome segments are shares of one cohort. Adjacent steps are separated by
 * ΔE ≈ 20 under deuteranopia and tritanopia, well clear of the 8 the check
 * asks for. The lighter steps sit under 3:1 against the page, so identity is
 * never left to colour alone: a legend is always present, segments carry their
 * own numbers, and the same figures are in the table below.
 */

/** Three steps of one ramp, dark to light — deeper in the funnel is darker. */
const SERIES = {
  applied: 'var(--color-accent-800)',
  replied: 'var(--color-accent-600)',
  interviews: 'var(--color-accent-400)',
} as const;

const OUTCOME_COLOURS: Record<string, string> = {
  open: 'var(--color-neutral-300)',
  accepted: 'var(--color-accent-900)',
  rejected: 'var(--color-accent-600)',
  ghosted: 'var(--color-accent-300)',
  withdrawn: 'var(--color-neutral-500)',
};

/**
 * A number set inside a fill is the one place text takes a colour from the
 * chart, and it takes the one that can be read on that fill — the page's ink on
 * the light steps, the page itself on the dark ones.
 */
const OUTCOME_INK: Record<string, string> = {
  open: 'var(--color-neutral-800)',
  accepted: 'var(--color-bg)',
  rejected: 'var(--color-bg)',
  ghosted: 'var(--color-accent-900)',
  withdrawn: 'var(--color-neutral-900)',
};

const OUTCOME_LABELS: Array<[keyof Stats['outcomes'], string]> = [
  ['open', 'still open'],
  ['accepted', 'accepted'],
  ['rejected', 'rejected'],
  ['ghosted', 'ghosted'],
  ['withdrawn', 'withdrawn'],
];

export function StatisticsView({
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

      <section style={{ marginBottom: 'var(--space-8)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>
          Applications a month, and what came back
        </div>
        <MonthlyChart months={stats.by_month} />
      </section>

      <section style={{ marginBottom: 'var(--space-8)' }}>
        <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>How they ended</div>
        <OutcomeBar outcomes={stats.outcomes} />
      </section>

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
              <div className="ratio-note">{stats.first_response.n} with a reply</div>
            </div>
            <div>
              <div style={{ font: '600 26px var(--font-heading)' }}>
                {stats.ghost.gate_met ? stats.ghost.display : '—'}
              </div>
              <div className="muted-55" style={{ fontSize: 12 }}>{stats.ghost.caption}</div>
              <div className="ratio-note">{stats.ghost.note}</div>
            </div>
          </div>
        </section>

        <section>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>
            Interview rate by channel
          </div>
          <ChannelChart channels={stats.channels} />
          <table className="table" style={{ marginTop: 'var(--space-4)' }}>
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
                style={{ display: 'grid', gridTemplateColumns: '128px 1fr 74px', gap: 'var(--space-3)', alignItems: 'center', marginBottom: 6 }}
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
                {/* `display` is formatted by the server. Printing `t.days` raw is
                    how this column came to read "12.416666666666666 d". */}
                <span style={{ fontSize: 12.5, textAlign: 'right' }} className={t.gate_met ? undefined : 'muted-50'}>
                  {t.gate_met ? t.display : `${t.n}/5`}
                </span>
              </div>
            ))
          )}
        </section>

        {stats.compensation.domain ? (
          <section>
            <div className="section-label" style={{ marginBottom: 'var(--space-3)' }}>Compensation</div>
            <Compensation compensation={stats.compensation} />
          </section>
        ) : null}
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
 * Volume a month, three nested series on one axis.
 *
 * One axis and one unit — applications — so the three series are directly
 * comparable and no second scale can flatter one of them. Columns are capped at
 * 22px and separated by the band's own leftover space.
 */
function MonthlyChart({ months }: { months: Stats['by_month'] }) {
  if (months.length === 0) {
    return <p className="muted-55" style={{ fontSize: 12.5 }}>Nothing applied for in this window yet.</p>;
  }

  const height = 200;
  const top = Math.max(...months.map((m) => m.applied), 1);
  // Clean ticks: the axis carries the values no label rides.
  const step = tickStep(top);
  const ceiling = Math.ceil(top / step) * step;
  const band = 100 / months.length;
  const barWidth = Math.min(7, (band - 2) / 3);

  return (
    <figure className="chart">
      <div className="chart-legend">
        {(
          [
            ['applied', 'Applied'],
            ['replied', 'Replied'],
            ['interviews', 'Interviewed'],
          ] as const
        ).map(([key, label]) => (
          <span key={key}>
            <i style={{ background: SERIES[key] }} />
            {label}
          </span>
        ))}
      </div>

      <div className="chart-plot" style={{ height }}>
        {Array.from({ length: ceiling / step + 1 }, (_, i) => i * step).map((value) => (
          <div key={value} className="chart-gridline" style={{ bottom: `${(value / ceiling) * 100}%` }}>
            <span>{value}</span>
          </div>
        ))}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label="Applications a month">
          {months.map((m, i) => {
            const left = i * band;
            const bars = [
              { key: 'applied' as const, n: m.applied },
              { key: 'replied' as const, n: m.replied },
              { key: 'interviews' as const, n: m.interviews },
            ];
            return bars.map((bar, j) => {
              const h = (bar.n / ceiling) * 100;
              return (
                <rect
                  key={`${m.month}-${bar.key}`}
                  x={left + 1 + j * (barWidth + 0.6)}
                  y={100 - h}
                  width={barWidth}
                  height={h}
                  fill={SERIES[bar.key]}
                >
                  <title>{`${m.label} · ${bar.n} ${bar.key}`}</title>
                </rect>
              );
            });
          })}
        </svg>
      </div>

      <div className="chart-axis">
        {months.map((m) => (
          <span key={m.month} style={{ width: `${band}%` }}>{m.label}</span>
        ))}
      </div>
      <figcaption className="muted-50">
        {months.reduce((a, m) => a + m.applied, 0)} applications, {months.reduce((a, m) => a + m.replied, 0)} with a
        human reply, {months.reduce((a, m) => a + m.interviews, 0)} that reached an interview. Every interview is
        also a reply, so the three series nest rather than sum.
      </figcaption>
    </figure>
  );
}

/** Part-to-whole, one row, 2px of surface between segments. */
function OutcomeBar({ outcomes }: { outcomes: Stats['outcomes'] }) {
  const total = OUTCOME_LABELS.reduce((a, [key]) => a + outcomes[key], 0);
  if (total === 0) {
    return <p className="muted-55" style={{ fontSize: 12.5 }}>Nothing in this window yet.</p>;
  }

  return (
    <figure className="chart">
      <div className="outcome-bar">
        {OUTCOME_LABELS.filter(([key]) => outcomes[key] > 0).map(([key, label]) => (
          <span
            key={key}
            style={{ flex: outcomes[key], background: OUTCOME_COLOURS[key], color: OUTCOME_INK[key] }}
            title={`${outcomes[key]} ${label}`}
          >
            {outcomes[key] / total > 0.08 ? outcomes[key] : null}
          </span>
        ))}
      </div>
      <div className="chart-legend" style={{ marginTop: 'var(--space-3)' }}>
        {OUTCOME_LABELS.map(([key, label]) => (
          <span key={key}>
            <i style={{ background: OUTCOME_COLOURS[key] }} />
            {label} · {outcomes[key]}
          </span>
        ))}
      </div>
      <figcaption className="muted-50">
        {total} applications in this window. Ghosted means closed by silence, with no rejection ever sent — the
        same set the ghost rate above is measured over.
      </figcaption>
    </figure>
  );
}

/** One measure, one hue, sorted by the thing being compared. */
function ChannelChart({ channels }: { channels: Stats['channels'] }) {
  const shown = channels.filter((c) => c.gate_met && c.iv_value !== null);
  if (shown.length === 0) {
    return (
      <p className="muted-55" style={{ fontSize: 12.5 }}>
        No channel has the {channels[0]?.note.split(' of ')[1] ?? '3 needed'} applications a rate needs yet.
      </p>
    );
  }
  const top = Math.max(...shown.map((c) => c.iv_value ?? 0), 0.01);

  return (
    <figure className="chart">
      {[...shown]
        .sort((a, b) => (b.iv_value ?? 0) - (a.iv_value ?? 0))
        .map((c) => (
          <div key={c.name} className="hbar">
            <span className="hbar-label">{c.name.replace(/_/g, ' ')}</span>
            <span className="hbar-track">
              <span
                className="hbar-fill"
                style={{ width: `${Math.max(1, ((c.iv_value ?? 0) / top) * 100)}%` }}
                title={`${c.interviews} of ${c.sent} reached an interview`}
              />
            </span>
            <span className="hbar-value">{c.iv}</span>
            <span className="hbar-note muted-50">{c.interviews}/{c.sent}</span>
          </div>
        ))}
    </figure>
  );
}

/** Three tracks over one shared scale, computed from a real min/max domain. */
function Compensation({ compensation }: { compensation: Stats['compensation'] }) {
  const { domain } = compensation;
  return (
    <Blueprint style={{ padding: 'var(--space-4)' }}>
      <Track label="Posted range">
        {compensation.posted.map((p, i) => (
          <span
            key={i}
            title={p.label}
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
        {compensation.ask ? (
          <span style={{ position: 'absolute', left: `${compensation.ask.at}%`, width: 2, height: 12, background: 'var(--color-text)' }} />
        ) : null}
      </Track>
      <Track label="Offers received">
        {compensation.offers.map((o, i) => (
          <span
            key={i}
            title={o.label}
            style={{ position: 'absolute', left: `${o.at}%`, width: 2, height: 12, background: 'var(--color-accent)' }}
          />
        ))}
      </Track>
      {domain ? (
        <div className="muted-50" style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11.5 }}>
          <span>{money(domain.min, domain.currency)}</span>
          <span>{money(domain.max, domain.currency)}</span>
        </div>
      ) : null}
      {compensation.dropped > 0 ? (
        <p className="muted-50" style={{ fontSize: 11.5, margin: 'var(--space-3) 0 0' }}>
          {compensation.dropped} figure{compensation.dropped === 1 ? '' : 's'} in another currency, listed
          separately rather than converted at a rate nobody chose.
        </p>
      ) : null}
    </Blueprint>
  );
}

function Track({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 'var(--space-4)' }}>
      <div className="muted-60" style={{ fontSize: 11.5, marginBottom: 4 }}>{label}</div>
      <div style={{ position: 'relative', height: 12 }}>
        <span style={{ position: 'absolute', inset: '5px 0 auto 0', height: 1, background: 'var(--color-divider)' }} />
        {children}
      </div>
    </div>
  );
}

/** 1, 2, 5, 10, 20, 50… — the axis gets round numbers or it gets none. */
function tickStep(top: number): number {
  const rough = top / 4;
  const magnitude = 10 ** Math.floor(Math.log10(Math.max(rough, 1)));
  for (const multiple of [1, 2, 5, 10]) {
    if (rough <= magnitude * multiple) return magnitude * multiple;
  }
  return magnitude * 10;
}

function money(minor: number, currency: string): string {
  return new Intl.NumberFormat('en-GB', { style: 'currency', currency, maximumFractionDigits: 0 }).format(minor / 100);
}
