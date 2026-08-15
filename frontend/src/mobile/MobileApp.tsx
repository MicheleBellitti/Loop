import { useState } from 'react';
import { CalendarDays, Inbox, LayoutList, Plus } from 'lucide-react';
import { Today } from './Today.js';
import { Pipeline } from './Pipeline.js';
import { Stats } from './Stats.js';
import { Detail } from './Detail.js';
import { QuickAddSheet } from '../sheets/QuickAdd.js';
import { ReviewQueue } from '../sheets/ReviewQueue.js';
import { DraftSheet } from '../sheets/DraftSheet.js';

/**
 * The mobile PWA at any phone width — the bezel in the prototype was
 * scaffolding.
 *
 * Client state is only ever ephemeral: active tab, open application, open
 * sheet. Everything else arrives precomputed.
 */

export type Tab = 'today' | 'pipeline' | 'stats';
export type Sheet =
  | { kind: 'add' }
  | { kind: 'review' }
  // A draft is asked for by suggestion when a card raised it, and by
  // application when the record did — most applications have no suggestion.
  | { kind: 'draft'; suggestionKey?: string; applicationId?: string }
  | null;

export function MobileApp() {
  const [tab, setTab] = useState<Tab>('today');
  const [openId, setOpenId] = useState<string | null>(null);
  const [sheet, setSheet] = useState<Sheet>(null);
  // The detail view returns to the previous tab with its scroll position kept.
  const [scrollMemory, setScrollMemory] = useState(0);

  const go = (next: Tab): void => {
    setTab(next);
    setOpenId(null);
    setSheet(null);
  };

  const open = (id: string): void => {
    setScrollMemory(document.querySelector('.phone-scroll')?.scrollTop ?? 0);
    setOpenId(id);
    setSheet(null);
  };

  const back = (): void => {
    setOpenId(null);
    requestAnimationFrame(() => {
      const el = document.querySelector('.phone-scroll');
      if (el) el.scrollTop = scrollMemory;
    });
  };

  return (
    <div className="phone">
      <div className="phone-scroll" key={openId ?? tab}>
        {openId ? (
          <Detail id={openId} onBack={back} onDraft={(applicationId) => setSheet({ kind: 'draft', applicationId })} />
        ) : tab === 'today' ? (
          <Today
            onOpen={open}
            onReview={() => setSheet({ kind: 'review' })}
            onDraft={(key) => setSheet({ kind: 'draft', suggestionKey: key })}
          />
        ) : tab === 'pipeline' ? (
          <Pipeline onOpen={open} />
        ) : (
          <Stats />
        )}
      </div>

      {!openId ? (
        <nav className="tabbar" aria-label="Sections">
          <TabButton icon={<CalendarDays size={20} strokeWidth={1.5} />} label="Today" active={tab === 'today'} onClick={() => go('today')} />
          <TabButton icon={<LayoutList size={20} strokeWidth={1.5} />} label="Pipeline" active={tab === 'pipeline'} onClick={() => go('pipeline')} />
          <TabButton icon={<Inbox size={20} strokeWidth={1.5} />} label="Stats" active={tab === 'stats'} onClick={() => go('stats')} />
          {/* "Add" opens a sheet rather than navigating. */}
          <TabButton icon={<Plus size={20} strokeWidth={1.5} />} label="Add" active={false} onClick={() => setSheet({ kind: 'add' })} />
        </nav>
      ) : null}

      {sheet?.kind === 'add' ? <QuickAddSheet onClose={() => setSheet(null)} /> : null}
      {sheet?.kind === 'review' ? <ReviewQueue onClose={() => setSheet(null)} /> : null}
      {sheet?.kind === 'draft' ? (
        <DraftSheet
          suggestionKey={sheet.suggestionKey}
          applicationId={sheet.applicationId}
          onClose={() => setSheet(null)}
        />
      ) : null}
    </div>
  );
}

function TabButton({
  icon,
  label,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      aria-current={active ? 'page' : undefined}
      style={{ color: active ? 'var(--color-accent)' : 'color-mix(in srgb, var(--color-text) 50%, transparent)' }}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
