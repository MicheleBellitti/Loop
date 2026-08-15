/**
 * F2 — watch lapsed. Degraded, not broken.
 *
 * "The app keeps working and says how stale it is. No dialog, no action." A
 * thin strip above an otherwise normal Today screen, and recently-found items
 * render dimmed while the backlog drains.
 */
export function CatchingUp({ backlog }: { backlog: number }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 'var(--space-3)',
        border: '1px solid var(--color-divider)',
        padding: 'var(--space-2) var(--space-3)',
        marginBottom: 'var(--space-4)',
      }}
      role="status"
    >
      <span style={{ width: 7, height: 7, background: 'var(--color-accent)', flex: 'none' }} />
      <span className="eyebrow" style={{ color: 'var(--color-accent-800)' }}>
        Catching up · {backlog} messages behind
      </span>
    </div>
  );
}

/**
 * F4 — model offline, rules still running.
 *
 * "Partial failure is named per component, so a stalled model never reads as
 * 'the app is broken'."
 */
export function ComponentStatus({
  components,
}: {
  components: { template_rules: string; calendar_detection: string; local_model: string };
}) {
  const rows: Array<[string, string]> = [
    ['Template rules', components.template_rules],
    ['Calendar detection', components.calendar_detection],
    ['Local model', components.local_model],
  ];
  return (
    <div style={{ border: '1px solid var(--color-divider)' }}>
      {rows.map(([label, status]) => {
        const bad = status !== 'running' && status !== 'reachable';
        return (
          <div
            key={label}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              padding: 'var(--space-3)',
              borderBottom: '1px solid var(--color-divider)',
              background: bad ? 'var(--color-accent-100)' : undefined,
            }}
          >
            <span style={{ fontSize: 13.5 }}>{label}</span>
            <span className={bad ? '' : 'muted-55'} style={{ fontSize: 12.5, color: bad ? 'var(--color-accent-800)' : undefined }}>
              {status === 'disabled' ? 'not configured' : status}
            </span>
          </div>
        );
      })}
    </div>
  );
}
