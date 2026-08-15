import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, setCsrf, type MailboxHealth } from './api.js';
import { useLiveUpdates } from './sse.js';
import { MobileApp } from './mobile/MobileApp.js';
import { Dashboard } from './desktop/Dashboard.js';
import { ChatWidget } from './chat/ChatWidget.js';
import { ViewingProvider } from './chat/viewing.js';
import { Onboarding } from './onboarding/Onboarding.js';
import { SignIn } from './SignIn.js';
import { AccessRevoked } from './states/AccessRevoked.js';

/**
 * The shell.
 *
 * Three gates, in this order: signed in → mailbox connected → the app. The
 * ordering is the onboarding design's whole argument — explanation precedes
 * consent, confirmation precedes any statistic — so a user who has not
 * connected anything never reaches a screen that shows a ratio.
 */

function useViewport(): 'mobile' | 'desktop' {
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const onResize = (): void => setWidth(window.innerWidth);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);
  // Below ~700px the dashboard defers to the mobile layout rather than
  // compressing further.
  return width < 700 ? 'mobile' : 'desktop';
}

export function App() {
  const queryClient = useQueryClient();
  const viewport = useViewport();

  const me = useQuery({
    queryKey: ['me'],
    queryFn: () => api.get<{ email: string; csrf: string; tz: string; locale: string }>('/api/me'),
    retry: false,
  });

  useEffect(() => {
    if (me.data?.csrf) setCsrf(me.data.csrf);
  }, [me.data?.csrf]);

  const authed = !!me.data;
  useLiveUpdates(queryClient, authed);

  const mailboxes = useQuery({
    queryKey: ['mailboxes'],
    queryFn: () => api.get<MailboxHealth>('/api/mailboxes'),
    enabled: authed,
  });

  if (me.isLoading) return <Splash />;
  if (!authed) return <SignIn onSignedIn={() => void me.refetch()} />;

  const health = mailboxes.data;

  // F1 — access revoked. The only full-screen failure, because it is the only
  // one the system cannot fix alone.
  if (health?.state === 'F1') return <AccessRevoked health={health} />;

  // No mailbox at all: that is not a failure, it is step one.
  if (mailboxes.isFetched && health && health.providers.length === 0) {
    return <Onboarding onDone={() => void mailboxes.refetch()} />;
  }

  // The assistant rides alongside either layout: same panel, same toggle, and
  // the provider between them is how it knows which record is open.
  return (
    <ViewingProvider>
      {viewport === 'mobile' ? <MobileApp /> : <Dashboard />}
      <ChatWidget />
    </ViewingProvider>
  );
}

function Splash() {
  return (
    <div style={{ display: 'grid', placeItems: 'center', height: '100%' }}>
      <div className="eyebrow">Loop</div>
    </div>
  );
}
