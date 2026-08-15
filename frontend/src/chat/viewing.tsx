import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';

/**
 * What the user is looking at, for the assistant to know about.
 *
 * The panel sits beside the record rather than inside it, so "why is this one
 * stuck?" is the natural question and the pronoun has no referent the model can
 * see. The detail views publish what they are showing; the panel reads it and
 * sends the id along with the message. React's own context, not a store
 * library: one nullable string shared between two siblings.
 */

const Viewing = createContext<{
  id: string | null;
  show: (id: string | null) => void;
}>({ id: null, show: () => undefined });

export function ViewingProvider({ children }: { children: ReactNode }) {
  const [id, show] = useState<string | null>(null);
  return <Viewing.Provider value={{ id, show }}>{children}</Viewing.Provider>;
}

/** Announce this application as on screen for as long as the caller renders. */
export function useShowing(id: string): void {
  const { show } = useContext(Viewing);
  useEffect(() => {
    show(id);
    return () => show(null);
  }, [id, show]);
}

export function useViewedApplication(): string | null {
  return useContext(Viewing).id;
}
