import { Component, type ErrorInfo, type ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Blueprint, Button } from '../components.js';

/**
 * The last resort.
 *
 * Without this, a throw anywhere in the tree unmounts the whole app and React
 * leaves an empty `<div id="root">` behind — a white page that names neither
 * what broke nor whether anything was lost. That is the one failure the product
 * must never present, because a user cannot tell it apart from data loss.
 *
 * Nothing shown here is recoverable state. The projection lives on the server
 * and every event is already appended, so a client crash costs a render and
 * nothing else — and the screen says exactly that, because the reassurance is
 * the point. The message is shown rather than swallowed: a console is not
 * somewhere a phone user can look, and a failure without provenance teaches
 * nobody when to trust the thing.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  override state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Kept for the developer; the user gets the screen below.
    console.error('render failed', error, info.componentStack);
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div style={{ padding: 'calc(env(safe-area-inset-top, 0px) + 32px) 18px 40px', maxWidth: 520, margin: '0 auto' }}>
        <Blueprint style={{ padding: 'var(--space-4)', borderColor: 'var(--color-accent-400)' }}>
          <div style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center', color: 'var(--color-accent-800)' }}>
            <AlertTriangle size={20} strokeWidth={1.5} />
            <strong style={{ font: '600 18px var(--font-heading)' }}>This screen failed to draw</strong>
          </div>
        </Blueprint>

        <p className="muted-72" style={{ fontSize: 14, lineHeight: 1.6, marginTop: 'var(--space-6)' }}>
          Nothing has been lost. Your applications and their whole history live on the server, not in
          this page — reloading rebuilds the view from them. If it fails again in the same place, the
          message below is the thing worth reporting.
        </p>

        <pre
          style={{
            border: '1px solid var(--color-divider)',
            padding: 'var(--space-4)',
            marginTop: 'var(--space-6)',
            fontSize: 12.5,
            lineHeight: 1.5,
            overflowX: 'auto',
            whiteSpace: 'pre-wrap',
          }}
        >
          {error.message || String(error)}
        </pre>

        <div style={{ display: 'grid', gap: 'var(--space-2)', marginTop: 'var(--space-6)' }}>
          <Button variant="primary" style={{ minHeight: 50 }} onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      </div>
    );
  }
}
