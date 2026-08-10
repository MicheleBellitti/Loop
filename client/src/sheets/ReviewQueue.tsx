import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, type ReviewItem } from '../api.js';
import { Blueprint, Button, Skeleton } from '../components.js';

/**
 * The review queue — the ~1% the agent could not place confidently.
 *
 * "Each answer is written back as a rule, so this queue shrinks over time
 * instead of growing." That sentence is the reason the screen exists, so it is
 * on the screen.
 */
export function ReviewQueue({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ['review'],
    queryFn: () => api.get<{ items: ReviewItem[] }>('/api/review'),
  });

  const answer = useMutation({
    mutationFn: (input: { id: string; choice: unknown }) =>
      api.post(`/api/review/${input.id}`, { choice: input.choice, learn: true }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['review'] });
      void queryClient.invalidateQueries({ queryKey: ['today'] });
    },
  });

  const items = data?.items ?? [];

  return (
    <div className="sheet-full" role="dialog" aria-label="Review queue">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <h1 className="headline" style={{ fontSize: 30 }}>
          Review queue · {items.length}
        </h1>
        <button onClick={onClose} className="eyebrow" style={{ background: 'none', border: 0, minHeight: 44 }}>
          Done
        </button>
      </div>

      <p className="muted-68" style={{ fontSize: 13, lineHeight: 1.55, marginBottom: 'var(--space-6)' }}>
        {items.length === 0
          ? 'Nothing needs you. Answers here are written back as rules, so this queue shrinks over time instead of growing.'
          : `${items.length === 1 ? 'One message' : `${items.length} messages`} the agent could not place confidently. Each answer is written back as a rule, so this queue shrinks over time instead of growing.`}
      </p>

      {isLoading ? <Skeleton height={160} /> : null}

      <div style={{ display: 'grid', gap: 'var(--space-4)' }}>
        {items.map((item) =>
          item.kind === 'ambiguous_match' ? (
            <Blueprint key={item.id} style={{ padding: 'var(--space-4)' }}>
              <div className="section-label">Same application?</div>
              <div className="muted-55" style={{ fontSize: 11.5, marginBottom: 'var(--space-3)' }}>
                {item.candidates.length} candidates within{' '}
                {closestMargin(item.candidates).toFixed(2)}
              </div>
              <Excerpt text={item.excerpt} />
              <div style={{ display: 'grid', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
                {item.candidates.map((c) => (
                  <button
                    key={c.application_id}
                    onClick={() => answer.mutate({ id: item.id, choice: { kind: 'application', application_id: c.application_id } })}
                    style={{
                      minHeight: 48,
                      textAlign: 'left',
                      border: '1px solid var(--color-divider)',
                      background: 'transparent',
                      padding: 'var(--space-2) var(--space-3)',
                    }}
                  >
                    <div style={{ fontSize: 14, fontWeight: 500 }}>{c.role_title}</div>
                    <div className="muted-55" style={{ fontSize: 11.5 }}>
                      {c.applied_at ? `applied ${new Date(c.applied_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}` : 'no date'} ·{' '}
                      {c.stage.replace(/_/g, ' ')} · cosine {c.cosine.toFixed(2)}
                    </div>
                  </button>
                ))}
                <Button onClick={() => answer.mutate({ id: item.id, choice: { kind: 'new_application' } })}>
                  Neither — new application
                </Button>
              </div>
            </Blueprint>
          ) : item.kind === 'merge_undo' ? (
            <Blueprint key={item.id} style={{ padding: 'var(--space-4)' }}>
              <div className="section-label">Merged automatically</div>
              <p className="muted-68" style={{ fontSize: 13, lineHeight: 1.5 }}>
                Two applications at this company looked like the same job, so they were merged into one
                with both sources kept. You can undo this for a fortnight.
              </p>
              <Button onClick={() => answer.mutate({ id: item.id, choice: { kind: 'undo_merge' } })}>
                Undo the merge
              </Button>
            </Blueprint>
          ) : (
            <div key={item.id} style={{ border: '1px solid var(--color-divider)', padding: 'var(--space-4)' }}>
              <div className="section-label">Unknown template</div>
              <div style={{ fontSize: 15, fontWeight: 500, margin: '2px 0 var(--space-3)' }}>
                Is this a rejection?
              </div>
              <Excerpt text={item.excerpt} />
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-2)', marginTop: 'var(--space-3)' }}>
                <Button
                  variant="primary"
                  onClick={() => answer.mutate({ id: item.id, choice: { kind: 'intent', intent: 'rejected', agree: true } })}
                >
                  Yes, rejected
                </Button>
                <Button
                  onClick={() => answer.mutate({ id: item.id, choice: { kind: 'intent', intent: 'rejected', agree: false } })}
                >
                  No, still live
                </Button>
              </div>
            </div>
          ),
        )}
      </div>
    </div>
  );
}

function Excerpt({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <blockquote
      style={{
        margin: 0,
        borderLeft: '2px solid var(--color-accent-300)',
        paddingLeft: 'var(--space-3)',
        fontSize: 12.5,
        lineHeight: 1.5,
      }}
      className="muted-70"
    >
      {text}
    </blockquote>
  );
}

function closestMargin(candidates: ReviewItem['candidates']): number {
  if (candidates.length < 2) return 0;
  const sorted = [...candidates].sort((a, b) => b.cosine - a.cosine);
  return Math.abs((sorted[0]?.cosine ?? 0) - (sorted[1]?.cosine ?? 0));
}
