import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type Draft } from '../api.js';
import { Button, Skeleton, Toast } from '../components.js';

/**
 * The follow-up draft.
 *
 * "Loop holds a read-only scope, so it cannot send this. Copying opens your own
 * mail client with the thread already selected." There is no send button here
 * because there is no send path anywhere in the system — a CI grep asserts it.
 *
 * Two ways in. A suggestion card names its own key; an application record names
 * itself, which is the case the drawer's "Draft follow-up" needed and did not
 * have — the suggestion route 404s for every application no nudge rule has
 * fired for, which is most of them.
 */
export function DraftSheet({
  suggestionKey,
  applicationId,
  onClose,
}: {
  suggestionKey?: string;
  applicationId?: string;
  onClose: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const path = suggestionKey
    ? `/api/suggestions/${encodeURIComponent(suggestionKey)}/draft`
    : `/api/applications/${applicationId}/draft`;

  const { data, isLoading, isError } = useQuery({
    queryKey: ['draft', path],
    queryFn: () => api.get<Draft>(path),
    retry: false,
  });

  const text = body ?? data?.body ?? '';

  const copyAndOpen = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(text);
      setToast('Copied. Opening your mail client…');
    } catch {
      setToast('Could not reach the clipboard — the draft is still here to select.');
    }
    if (data?.mailto_url) window.location.href = data.mailto_url;
  };

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="sheet" role="dialog" aria-label="Follow-up draft">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <h2 className="headline" style={{ fontSize: 28 }}>
            Follow-up draft
          </h2>
          <button onClick={onClose} className="eyebrow" style={{ background: 'none', border: 0, minHeight: 44 }}>
            Close
          </button>
        </div>

        {isError ? (
          // Better than a skeleton that spins for ever, which is what a 404
          // used to produce here.
          <p className="muted-70" style={{ fontSize: 13.5, lineHeight: 1.6 }}>
            No draft could be composed for this one — it has no company or no
            history to refer to. Nothing was changed.
          </p>
        ) : isLoading || !data ? (
          <Skeleton height={140} />
        ) : (
          <>
            <p className="muted-55" style={{ fontSize: 11.5, margin: '0 0 var(--space-3)' }}>
              {data.subject} · written in the thread’s language
            </p>

            {editing ? (
              <textarea
                value={text}
                onChange={(e) => setBody(e.target.value)}
                rows={8}
                style={{
                  width: '100%',
                  border: '1px solid var(--color-divider)',
                  background: 'transparent',
                  padding: 'var(--space-4)',
                  fontSize: 14,
                  lineHeight: 1.6,
                  font: 'inherit',
                  color: 'inherit',
                  borderRadius: 0,
                }}
              />
            ) : (
              <div
                style={{
                  border: '1px solid var(--color-divider)',
                  padding: 'var(--space-4)',
                  fontSize: 14,
                  lineHeight: 1.6,
                  whiteSpace: 'pre-wrap',
                }}
              >
                {text}
              </div>
            )}

            <p className="muted-50" style={{ fontSize: 11.5, lineHeight: 1.5, marginTop: 'var(--space-3)' }}>
              {data.note} Copying opens your own mail client with the thread already selected.
            </p>

            <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr', gap: 'var(--space-2)', marginTop: 'var(--space-4)' }}>
              <Button variant="primary" onClick={() => void copyAndOpen()}>
                Copy &amp; open thread
              </Button>
              <Button onClick={() => setEditing((v) => !v)}>{editing ? 'Done' : 'Edit'}</Button>
            </div>
          </>
        )}
      </div>
      {toast ? <Toast message={toast} onDismiss={() => setToast(null)} /> : null}
    </>
  );
}
