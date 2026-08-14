import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ChevronLeft } from 'lucide-react';
import { api, type ApplicationDetail } from '../api.js';
import { Button, Mark, Provenance, Skeleton, StagePicker, Tag, Toast } from '../components.js';
import { failure } from '../desktop/Drawer.js';

/**
 * The application record. The event log is the core of the screen: every
 * automated event exposes its source and its confidence, which is the mechanism
 * by which a user learns when to trust the system.
 */
export function Detail({
  id,
  onBack,
  onDraft,
}: {
  id: string;
  onBack: () => void;
  /** The application to draft for — not a suggestion key, which most rows lack. */
  onDraft: (applicationId: string) => void;
}) {
  const queryClient = useQueryClient();
  const [correcting, setCorrecting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ['application', id],
    queryFn: () => api.get<ApplicationDetail>(`/api/applications/${id}`),
  });

  const correct = useMutation({
    mutationFn: (to: string) => api.post(`/api/applications/${id}/correct`, { field: 'stage', to }),
    onSuccess: () => {
      setCorrecting(false);
      setToast('Correction recorded. The agent will not overwrite it.');
      void queryClient.invalidateQueries({ queryKey: ['application', id] });
      void queryClient.invalidateQueries({ queryKey: ['applications'] });
      void queryClient.invalidateQueries({ queryKey: ['today'] });
      void queryClient.invalidateQueries({ queryKey: ['stats'] });
    },
    onError: (error) => setToast(failure(error, 'That did not save. Nothing was changed.')),
  });

  if (isLoading || !data) {
    return (
      <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
        <Skeleton width={80} height={20} />
        <Skeleton width="70%" height={36} />
        <Skeleton height={120} />
        <Skeleton height={200} />
      </div>
    );
  }

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--space-4)' }}>
        <button
          onClick={onBack}
          style={{ display: 'flex', alignItems: 'center', gap: 4, minHeight: 44, background: 'none', border: 0, paddingLeft: 0 }}
        >
          <ChevronLeft size={18} strokeWidth={1.5} />
          <span className="eyebrow">Back</span>
        </button>
        <span className="eyebrow">tracked automatically</span>
      </div>

      <h1 className="headline" style={{ fontSize: 36 }}>
        {data.company}
      </h1>
      <p className="muted-72" style={{ fontSize: 15, margin: '2px 0 var(--space-3)' }}>
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
        <Fact
          label="Posted range"
          value={
            data.facts.posted_range
              ? `${money(data.facts.posted_range.min_minor, data.facts.posted_range.currency)}–${money(data.facts.posted_range.max_minor ?? data.facts.posted_range.min_minor, data.facts.posted_range.currency)}`
              : 'not posted'
          }
          top
        />
      </div>

      <section>
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
                <div className="muted-65" style={{ fontSize: 12.5, marginTop: 1 }}>
                  {e.detail}
                </div>
              ) : null}
              <div style={{ marginTop: 5 }}>
                <Provenance source={e.source} conf={e.conf} />
              </div>
            </div>
          </div>
        ))}
      </section>

      <div style={{ display: 'grid', gap: 'var(--space-2)', marginTop: 'var(--space-6)' }}>
        <Button variant="primary" style={{ minHeight: 48 }} onClick={() => onDraft(id)}>
          Draft a follow-up
        </Button>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-2)' }}>
          <Button
            onClick={() => {
              if (data.facts.posting_url) window.open(data.facts.posting_url, '_blank', 'noopener,noreferrer');
            }}
            disabled={!data.facts.posting_url}
          >
            Open posting
          </Button>
          <Button aria-expanded={correcting} onClick={() => setCorrecting((v) => !v)}>
            Correct stage
          </Button>
        </div>
      </div>

      {correcting ? (
        <StagePicker
          busy={correct.isPending}
          current={data.stage}
          onPick={(key) => correct.mutate(key)}
          note="Correcting the stage writes a human_corrected event at confidence 1.0 — the agent will not overwrite it."
        />
      ) : null}

      {toast ? <Toast message={toast} onDismiss={() => setToast(null)} /> : null}
    </>
  );
}

function Fact({
  label,
  value,
  border = false,
  top = false,
}: {
  label: string;
  value: string;
  border?: boolean;
  top?: boolean;
}) {
  return (
    <div
      style={{
        padding: 'var(--space-3)',
        borderRight: border ? '1px solid var(--color-divider)' : undefined,
        borderTop: top ? '1px solid var(--color-divider)' : undefined,
      }}
    >
      <div
        className="muted-50"
        style={{ font: '600 10px var(--font-heading)', letterSpacing: '.11em', textTransform: 'uppercase' }}
      >
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
