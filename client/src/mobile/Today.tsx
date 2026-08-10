import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Inbox } from 'lucide-react';
import { api, type Today as TodayData } from '../api.js';
import { Blueprint, Button, Mark, Skeleton, Tag } from '../components.js';
import { CatchingUp } from '../states/CatchingUp.js';

/**
 * Today — "the only screen a user needs on an ordinary day: what happened, what
 * needs them, nothing else. It must read as calm when there is nothing to do."
 */
export function Today({
  onOpen,
  onReview,
  onDraft,
}: {
  onOpen: (id: string) => void;
  onReview: () => void;
  onDraft: (key: string) => void;
}) {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['today'],
    queryFn: () => api.get<TodayData>('/api/today'),
  });

  const dismiss = useMutation({
    mutationFn: (key: string) => api.post(`/api/suggestions/${encodeURIComponent(key)}/snooze`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['today'] }),
  });

  if (isLoading || !data) return <TodaySkeleton />;

  const { counters, mailbox_health: health } = data;

  return (
    <>
      {/* F2 — degraded, not broken. A strip above an otherwise normal screen. */}
      {health.state === 'F2' ? <CatchingUp backlog={health.backlog} /> : null}

      <header className="today-header">
        <div>
          <div className="eyebrow">{data.eyebrow}</div>
          <h1 className="headline">
            {data.headline.map((line, i) => (
              <span key={i} style={{ display: 'block' }}>
                {line}
              </span>
            ))}
          </h1>
        </div>
        {data.review_count > 0 ? (
          <button className="review-button" onClick={onReview} aria-label={`Review queue, ${data.review_count} waiting`}>
            <Inbox size={18} strokeWidth={1.5} />
            <span className="review-badge">{data.review_count}</span>
          </button>
        ) : null}
      </header>

      <div className="counters">
        <div>
          <div className="counter-n">{counters.live}</div>
          <div className="counter-label">live</div>
        </div>
        <div className="counter-interviewing">
          <div className="counter-n">{counters.interviewing}</div>
          <div className="counter-label">interviewing</div>
        </div>
        <div className="counter-offer">
          <div className="counter-n">{counters.offer}</div>
          <div className="counter-label">{counters.offer === 1 ? 'offer' : 'offers'}</div>
        </div>
      </div>

      {data.next_interview ? (
        <Blueprint>
          <button className="next-up" onClick={() => onOpen(data.next_interview!.application_id)}>
            <div className="eyebrow" style={{ color: 'var(--color-accent-700)' }}>
              Next up
            </div>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginTop: 2 }}>
              <span className="next-up-company">{data.next_interview.company}</span>
              <span className="next-up-time">{relativeDay(data.next_interview.starts_at)}</span>
            </div>
            <div style={{ fontSize: 13.5 }} className="muted-72">
              {data.next_interview.stage} · {data.next_interview.role}
            </div>
            <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 'var(--space-3)', flexWrap: 'wrap' }}>
              {data.next_interview.rounds_done > 0 ? (
                <Tag tone="accent">{data.next_interview.rounds_done} rounds done</Tag>
              ) : null}
              {/* Provenance on every automatically-derived claim. */}
              <Tag>{data.next_interview.provenance}</Tag>
            </div>
          </button>
        </Blueprint>
      ) : null}

      {data.suggestions.length > 0 ? (
        <section style={{ marginTop: 'var(--space-6)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 'var(--space-3)' }}>
            <span className="section-label">Suggested</span>
            <span className="eyebrow">{data.suggestions.length} left</span>
          </div>
          <div style={{ display: 'grid', gap: 'var(--space-3)' }}>
            {data.suggestions.map((s) => (
              <article className="suggestion" key={s.key}>
                <div style={{ display: 'flex', justifyContent: 'space-between', gap: 'var(--space-3)' }}>
                  <Tag tone="accent">{s.kind}</Tag>
                  <span className="muted-55" style={{ fontSize: 11.5 }}>
                    {s.meta}
                  </span>
                </div>
                <h2 className="suggestion-title">{s.title}</h2>
                <p className="suggestion-body" style={{ margin: 0 }}>
                  {s.body}
                </p>
                <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
                  <Button
                    variant="primary"
                    style={{ flex: 1 }}
                    onClick={() => {
                      if (s.rule === 'follow_up_due') onDraft(s.key);
                      else if (s.applicationIds[0]) onOpen(s.applicationIds[0]);
                    }}
                  >
                    {s.cta}
                  </Button>
                  <Button onClick={() => dismiss.mutate(s.key)}>Later</Button>
                </div>
              </article>
            ))}
          </div>
        </section>
      ) : null}

      {data.recent_events.length > 0 ? (
        <section style={{ marginTop: 'var(--space-6)' }}>
          <div className="section-label" style={{ marginBottom: 'var(--space-2)' }}>
            Picked up this week
          </div>
          {data.recent_events.map((e, i) => (
            <button key={i} className="activity-row" onClick={() => onOpen(e.application_id)}>
              <Mark muted={e.closed} />
              <span style={{ flex: 1 }}>
                <span style={{ display: 'block', fontSize: 15, fontWeight: 500 }}>{e.company}</span>
                <span className="muted-60" style={{ fontSize: 12.5 }}>
                  {e.what}
                </span>
              </span>
              <span className="muted-45" style={{ font: '600 11px var(--font-heading)', letterSpacing: '.08em', textTransform: 'uppercase' }}>
                {e.when}
              </span>
            </button>
          ))}
        </section>
      ) : null}

      {/* E1 — day one. Empty must read as working, not broken. */}
      {data.headline_kind === 'empty' ? (
        <section style={{ marginTop: 'var(--space-6)' }}>
          <Blueprint style={{ padding: 'var(--space-4)' }}>
            <div style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center' }}>
              <span style={{ width: 7, height: 7, background: 'var(--color-accent)' }} />
              <strong style={{ fontSize: 14 }}>Watching your mailbox</strong>
            </div>
            <p className="muted-70" style={{ fontSize: 13, lineHeight: 1.55, margin: 'var(--space-3) 0 0' }}>
              Last read {health.minutes_since_read ?? 0} minutes ago. The moment a confirmation or an
              auto-reply arrives, it appears here on its own — you do not need to come back and check.
            </p>
          </Blueprint>
        </section>
      ) : null}

      <p className="muted-55" style={{ fontSize: 12.5, lineHeight: 1.55, marginTop: 'var(--space-6)' }}>
        {/* The product's thesis. */}
        {data.closing_line}
      </p>

      {health.state === 'ok' && !health.connected ? (
        <p style={{ fontSize: 12.5, color: 'var(--color-accent-800)', display: 'flex', gap: 6, alignItems: 'center' }}>
          <AlertTriangle size={14} strokeWidth={1.5} /> No mailbox is connected.
        </p>
      ) : null}
    </>
  );
}

function relativeDay(iso: string): string {
  const then = new Date(iso);
  const days = Math.round((then.getTime() - Date.now()) / 86_400_000);
  const time = then.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  if (days <= 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  return `${then.toLocaleDateString('en-GB', { weekday: 'long' })} ${time}`;
}

function TodaySkeleton() {
  return (
    <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
      <Skeleton width={120} height={11} />
      <Skeleton width="70%" height={34} />
      <Skeleton height={90} />
      <Skeleton height={120} />
      <Skeleton height={56} />
      <Skeleton height={56} />
    </div>
  );
}
