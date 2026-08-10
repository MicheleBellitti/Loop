import { useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api.js';
import { Button, Toast } from '../components.js';

/**
 * Quick add — "the fallback for applications that never generate an email at
 * all". Posting URL first: everything after that point is automatic.
 */
export function QuickAddSheet({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient();
  const [url, setUrl] = useState('');
  const [company, setCompany] = useState('');
  const [role, setRole] = useState('');
  const [channel, setChannel] = useState('career_page');
  const [appliedAt, setAppliedAt] = useState(() => new Date().toISOString().slice(0, 10));
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () =>
      url.trim()
        ? api.post('/api/applications', { posting_url: url.trim() })
        : api.post('/api/applications', {
            company,
            role,
            channel,
            applied_at: new Date(appliedAt).toISOString(),
          }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['today'] });
      void queryClient.invalidateQueries({ queryKey: ['applications'] });
      onClose();
    },
    onError: () => setError('That did not save. Nothing was created.'),
  });

  const canSubmit = url.trim().length > 0 || (company.trim() && role.trim());

  return (
    <>
      <div className="scrim" onClick={onClose} />
      <div className="sheet" role="dialog" aria-label="Add an application">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <h2 className="headline" style={{ fontSize: 28 }}>
            Add manually
          </h2>
          <button onClick={onClose} className="eyebrow" style={{ background: 'none', border: 0, minHeight: 44 }}>
            Close
          </button>
        </div>

        <p className="muted-68" style={{ fontSize: 13, lineHeight: 1.5 }}>
          Paste the posting URL and the rest is fetched: company, role, location, posted range and the
          ATS behind it. Everything after this point is automatic.
        </p>

        <Field label="Posting URL">
          <input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            inputMode="url"
            placeholder="https://…"
            style={inputStyle}
          />
        </Field>

        {!url.trim() ? (
          <>
            <Field label="Company">
              <input value={company} onChange={(e) => setCompany(e.target.value)} style={inputStyle} />
            </Field>
            <Field label="Role">
              <input value={role} onChange={(e) => setRole(e.target.value)} style={inputStyle} />
            </Field>
            <Field label="Channel">
              <select value={channel} onChange={(e) => setChannel(e.target.value)} style={inputStyle}>
                <option value="career_page">Career page</option>
                <option value="linkedin">LinkedIn</option>
                <option value="indeed">Indeed</option>
                <option value="referral">Referral</option>
                <option value="recruiter">Recruiter</option>
                <option value="other">Other</option>
              </select>
            </Field>
            <Field label="Applied on">
              <input type="date" value={appliedAt} onChange={(e) => setAppliedAt(e.target.value)} style={inputStyle} />
            </Field>
          </>
        ) : null}

        <Button
          variant="primary"
          style={{ width: '100%', minHeight: 50, marginTop: 'var(--space-4)' }}
          disabled={!canSubmit || create.isPending}
          onClick={() => create.mutate()}
        >
          {create.isPending ? 'Tracking…' : 'Track it'}
        </Button>
      </div>
      {error ? <Toast message={error} onDismiss={() => setError(null)} /> : null}
    </>
  );
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  minHeight: 44,
  border: '1px solid var(--color-divider)',
  background: 'transparent',
  padding: 'var(--space-3)',
  font: 'inherit',
  color: 'inherit',
  borderRadius: 0,
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: 'block', marginTop: 'var(--space-4)' }}>
      <span className="eyebrow" style={{ display: 'block', marginBottom: 4 }}>
        {label}
      </span>
      {children}
    </label>
  );
}
