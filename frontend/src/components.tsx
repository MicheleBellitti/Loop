import type { ReactNode } from 'react';

/**
 * The pieces the Industry system defines and every screen reuses.
 */

/**
 * The blueprint frame. "Do not drop the registration marks from a framed
 * element" — so the marks are part of the component and cannot be forgotten at
 * a call site.
 */
export function Blueprint({
  children,
  style,
  className = '',
}: {
  children: ReactNode;
  style?: React.CSSProperties;
  className?: string;
}) {
  return (
    <div className={`blueprint ${className}`} style={style}>
      <i className="corner tl" />
      <i className="corner tr" />
      <i className="corner bl" />
      <i className="corner br" />
      {children}
    </div>
  );
}

/** The 11px two-path registration glyph that leads every derived claim. */
export function Mark({ muted = false }: { muted?: boolean }) {
  return (
    <svg className={`mark ${muted ? 'mark-muted' : ''}`} viewBox="0 0 11 11" fill="none" aria-hidden="true">
      <path d="M5.5 0v11" />
      <path d="M0 5.5h11" />
    </svg>
  );
}

export function Button({
  variant = 'secondary',
  children,
  style,
  ...rest
}: {
  variant?: 'primary' | 'secondary' | 'ghost';
  children: ReactNode;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const base: React.CSSProperties = {
    minHeight: 44,
    padding: '0 var(--space-4)',
    border: '1px solid var(--color-divider)',
    background: 'transparent',
    font: '600 13px var(--font-heading)',
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
  };
  const primary: React.CSSProperties = {
    // The primary button is the one solid object on the board.
    background: 'var(--color-accent)',
    color: 'var(--color-bg)',
    borderColor: 'var(--color-accent)',
  };
  const ghost: React.CSSProperties = { border: 0 };
  // "Disabled controls drop to 45% opacity" — the design system says so, and a
  // primary button that looks pressable but does nothing is worse than one that
  // is visibly out of reach.
  const disabled: React.CSSProperties = rest.disabled
    ? { opacity: 0.45, cursor: 'not-allowed' }
    : {};
  return (
    <button
      {...rest}
      aria-disabled={rest.disabled || undefined}
      style={{
        ...base,
        ...(variant === 'primary' ? primary : {}),
        ...(variant === 'ghost' ? ghost : {}),
        ...disabled,
        ...style,
      }}
    >
      {children}
    </button>
  );
}

export function Tag({
  tone = 'neutral',
  children,
}: {
  tone?: 'accent' | 'neutral';
  children: ReactNode;
}) {
  return (
    <span
      style={{
        display: 'inline-flex',
        padding: '2px var(--space-3)',
        fontSize: 11.5,
        border: '1px solid',
        borderColor: tone === 'accent' ? 'var(--color-accent-400)' : 'var(--color-divider)',
        background: tone === 'accent' ? 'var(--color-accent-100)' : 'transparent',
        color: tone === 'accent' ? 'var(--color-accent-800)' : 'color-mix(in srgb, var(--color-text) 65%, transparent)',
      }}
    >
      {children}
    </span>
  );
}

/**
 * Provenance chip. Shown on every automatically-derived claim, with the
 * confidence beside it: this is the mechanism by which a user learns when to
 * trust the system.
 */
export function Provenance({ source, conf }: { source: string; conf?: string }) {
  return (
    <span style={{ display: 'inline-flex', gap: 'var(--space-2)', alignItems: 'center' }}>
      <span className="provenance">{source}</span>
      {conf ? <span className="conf">conf {conf}</span> : null}
    </span>
  );
}

/**
 * A gated figure. Below its threshold the number is withheld and the gate is
 * named — which turns an empty chart into a progress bar rather than a
 * disappointment.
 */
export function Figure({
  label,
  value,
  note,
  gateMet = true,
}: {
  label: string;
  value: string;
  note: string;
  gateMet?: boolean;
}) {
  return (
    <div style={{ border: '1px solid var(--color-divider)', padding: 'var(--space-4)' }}>
      <div
        style={{
          font: '600 13px var(--font-heading)',
          letterSpacing: '0.06em',
          textTransform: 'uppercase',
          color: 'color-mix(in srgb, var(--color-text) 70%, transparent)',
        }}
      >
        {label}
      </div>
      <div className="ratio-value">{gateMet ? value : '—'}</div>
      <div className="ratio-note">{note}</div>
    </div>
  );
}

export function Skeleton({ width = '100%', height = 14 }: { width?: string | number; height?: number }) {
  return <div className="skeleton" style={{ width, height }} />;
}

/**
 * The eleven stage keys, in depth order.
 *
 * The client is not allowed to *derive* a stage — that is the stage machine's
 * job and it lives on the server — but correcting one means naming one, and
 * there is no endpoint that lists them. This is the same list the mobile detail
 * view has always carried, lifted so the two cannot disagree about what you are
 * allowed to say happened.
 */
export const STAGE_KEYS = [
  'applied',
  'acknowledged',
  'recruiter_reachout',
  'hr_call',
  'take_home',
  'technical',
  'system_design',
  'onsite_loop',
  'final',
  'offer',
  'negotiating',
] as const;

export function StagePicker({
  current,
  onPick,
  busy = false,
  note,
}: {
  current?: string;
  onPick: (key: string) => void;
  busy?: boolean;
  note?: string;
}) {
  return (
    <div style={{ marginTop: 'var(--space-3)', border: '1px solid var(--color-divider)', padding: 'var(--space-3)' }}>
      <div className="eyebrow" style={{ marginBottom: 'var(--space-2)' }}>
        Set the stage to
      </div>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--space-2)' }}>
        {STAGE_KEYS.map((key) => (
          <button
            key={key}
            className="filter-chip"
            aria-pressed={key === current}
            onClick={() => onPick(key)}
            disabled={busy || key === current}
            style={key === current ? undefined : { opacity: busy ? 0.45 : 1 }}
          >
            {key.replace(/_/g, ' ')}
          </button>
        ))}
      </div>
      {note ? (
        <p className="muted-50" style={{ fontSize: 11.5, lineHeight: 1.5, margin: 'var(--space-3) 0 0' }}>
          {note}
        </p>
      ) : null}
    </div>
  );
}

export function Toast({
  message,
  action,
  onAction,
  onDismiss,
}: {
  message: string;
  action?: string;
  onAction?: () => void;
  onDismiss: () => void;
}) {
  return (
    <div className="toast" role="status">
      <span>{message}</span>
      {action ? (
        <button
          onClick={onAction}
          style={{
            background: 'none',
            border: 0,
            color: 'var(--color-accent-300)',
            font: '600 12px var(--font-heading)',
            letterSpacing: '.06em',
            textTransform: 'uppercase',
          }}
        >
          {action}
        </button>
      ) : null}
      <button onClick={onDismiss} aria-label="Dismiss" style={{ background: 'none', border: 0, color: 'inherit' }}>
        ×
      </button>
    </div>
  );
}
