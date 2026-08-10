import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api.js';
import { Blueprint, Button, Mark } from '../components.js';

/**
 * Seven steps, and the whole flow exists to earn one permission.
 *
 * "The ordering is the design: explanation precedes consent, and confirmation
 * precedes any statistic." Step 2 is not skippable — there is no way past it
 * that does not go through the button that names the scopes.
 */

type Step = 0 | 1 | 2 | 3 | 4 | 5 | 6;

const STEPS: Array<{ n: string; title: string; note: string }> = [
  { n: '01', title: 'Welcome', note: 'what this is' },
  { n: '02', title: 'What Loop reads', note: 'the honest version' },
  { n: '03', title: 'Google consent', note: 'leaving the app' },
  { n: '04', title: 'History depth', note: 'how far back' },
  { n: '05', title: 'First scan', note: 'watch it work' },
  { n: '06', title: 'Confirm the haul', note: 'your approval' },
  { n: '07', title: 'Notifications', note: 'the four reasons' },
];

export function Onboarding({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>(0);
  const [months, setMonths] = useState(12);
  const queryClient = useQueryClient();

  const start = useMutation({
    mutationFn: () => api.post<{ url: string }>('/api/mailboxes/gmail/start'),
    onSuccess: (res) => {
      window.location.href = res.url;
    },
  });

  const backfill = useMutation({
    mutationFn: () => api.post('/api/mailboxes/backfill', { months }),
    onSuccess: () => setStep(4),
  });

  const next = (): void => setStep((s) => Math.min(6, s + 1) as Step);
  const back = (): void => setStep((s) => Math.max(0, s - 1) as Step);

  return (
    <div className="phone">
      <div className="phone-scroll">
        {step === 0 ? <Welcome /> : null}
        {step === 1 ? <WhatItReads /> : null}
        {step === 2 ? <Consent /> : null}
        {step === 3 ? <Depth months={months} setMonths={setMonths} /> : null}
        {step === 4 ? <FirstScan /> : null}
        {step === 5 ? <ConfirmHaul /> : null}
        {step === 6 ? <Notifications /> : null}
      </div>

      <footer style={{ padding: '0 18px calc(env(safe-area-inset-bottom, 0px) + 16px)' }}>
        <div style={{ display: 'flex', gap: 3, marginBottom: 'var(--space-3)' }}>
          {STEPS.map((_, i) => (
            <span
              key={i}
              style={{
                flex: 1,
                height: 3,
                background: i <= step ? 'var(--color-accent)' : 'var(--color-divider)',
              }}
            />
          ))}
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: step === 0 ? '1fr' : 'auto 1fr', gap: 'var(--space-2)' }}>
          {step > 0 ? <Button onClick={back} style={{ minHeight: 50 }}>Back</Button> : null}
          <Button
            variant="primary"
            style={{ minHeight: 50 }}
            disabled={start.isPending || backfill.isPending}
            onClick={() => {
              if (step === 2) start.mutate();
              else if (step === 3) backfill.mutate();
              else if (step === 6) {
                void queryClient.invalidateQueries({ queryKey: ['mailboxes'] });
                onDone();
              } else next();
            }}
          >
            {step === 0
              ? 'Connect a mailbox'
              : step === 2
                ? 'Continue to Google'
                : step === 3
                  ? 'Start the first scan'
                  : step === 6
                    ? 'Finish'
                    : 'Next'}
          </Button>
        </div>
        {step === 0 ? (
          <p className="muted-55" style={{ fontSize: 12.5, textAlign: 'center', marginTop: 'var(--space-2)' }}>
            or add applications by hand
          </p>
        ) : null}
      </footer>
    </div>
  );
}

function Welcome() {
  return (
    <>
      <h1 className="headline" style={{ fontSize: 40 }}>
        <span style={{ display: 'block' }}>You already</span>
        <span style={{ display: 'block' }}>sent them</span>
      </h1>
      <p className="muted-72" style={{ fontSize: 14.5, lineHeight: 1.6, marginTop: 'var(--space-4)' }}>
        Every application you have ever submitted left a trace in your inbox: a confirmation, an
        auto-reply, an invitation, a no. Loop reads those traces and keeps the pipeline for you.
      </p>
      <ul style={{ listStyle: 'none', padding: 0, margin: 'var(--space-6) 0 0', display: 'grid', gap: 'var(--space-3)' }}>
        {[
          'No spreadsheet to update, ever',
          'Works with LinkedIn, Indeed and any company career page',
          'Runs on your own machine — nothing is sent anywhere',
        ].map((line) => (
          <li key={line} style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center', fontSize: 14 }}>
            <Mark />
            {line}
          </li>
        ))}
      </ul>
    </>
  );
}

function WhatItReads() {
  return (
    <>
      <div className="eyebrow">Step 2 of 7</div>
      <h1 className="headline" style={{ fontSize: 34, marginBottom: 'var(--space-3)' }}>
        What Loop reads
      </h1>
      <p className="muted-72" style={{ fontSize: 14, lineHeight: 1.6 }}>
        Read this before you grant anything. It is the honest version, not the reassuring one.
      </p>

      <div style={{ border: '1px solid var(--color-divider)', marginTop: 'var(--space-6)' }}>
        <div style={{ background: 'var(--color-accent-100)', padding: 'var(--space-2) var(--space-3)' }}>
          <span className="section-label" style={{ color: 'var(--color-accent-800)' }}>It can</span>
        </div>
        <ul style={{ margin: 0, padding: 'var(--space-3) var(--space-3) var(--space-3) 28px', fontSize: 13.5, lineHeight: 1.6 }}>
          <li>Read message headers and bodies, to find applications</li>
          <li>Read calendar events, to detect interviews</li>
          <li>Keep the fields it extracts: company, role, stage, dates</li>
        </ul>
      </div>

      {/* The cannot-box carries equal weight. */}
      <div style={{ border: '1px solid var(--color-divider)', marginTop: 'var(--space-4)' }}>
        <div style={{ padding: 'var(--space-2) var(--space-3)', borderBottom: '1px solid var(--color-divider)' }}>
          <span className="section-label">It cannot</span>
        </div>
        <ul style={{ margin: 0, padding: 'var(--space-3) var(--space-3) var(--space-3) 28px', fontSize: 13.5, lineHeight: 1.6 }}>
          <li>Send, reply, delete or label anything — the scope is read-only</li>
          <li>Store your email bodies: they live in memory for one parse</li>
          <li>Reach your contacts, Drive, or anything outside mail and calendar</li>
        </ul>
      </div>

      <div style={{ marginTop: 'var(--space-4)' }}>
        <span className="eyebrow">Scopes requested</span>
        <div style={{ display: 'flex', gap: 'var(--space-2)', marginTop: 4 }}>
          <code style={codeChip}>gmail.readonly</code>
          <code style={codeChip}>calendar.readonly</code>
        </div>
      </div>
    </>
  );
}

const codeChip: React.CSSProperties = {
  border: '1px solid var(--color-divider)',
  padding: '2px var(--space-2)',
  fontSize: 12,
};

function Consent() {
  return (
    <>
      <div className="eyebrow">Step 3 of 7 · leaving the app</div>
      <h1 className="headline" style={{ fontSize: 30, marginBottom: 'var(--space-4)' }}>
        What Google will ask
      </h1>
      {/* A representation of the real system screen, including the unverified
          warning — explained before the user meets it. */}
      <Blueprint style={{ padding: 'var(--space-4)' }}>
        <div className="muted-55" style={{ fontSize: 11.5 }}>accounts.google.com</div>
        <div style={{ fontSize: 15, fontWeight: 500, margin: 'var(--space-2) 0' }}>
          Loop (self-hosted) wants access to your Google Account
        </div>
        <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.6 }} className="muted-70">
          <li>View your email messages and settings</li>
          <li>See and download any calendar you can access</li>
        </ul>
        <p className="muted-55" style={{ fontSize: 12, lineHeight: 1.5, marginTop: 'var(--space-3)' }}>
          This app is unverified because it runs on your own server. You can revoke access at any time
          in your Google Account.
        </p>
      </Blueprint>
      <p className="muted-60" style={{ fontSize: 12.5, lineHeight: 1.55, marginTop: 'var(--space-4)' }}>
        Loop never sees your password. The token it receives is encrypted with a key that lives outside
        the database.
      </p>
    </>
  );
}

function Depth({ months, setMonths }: { months: number; setMonths: (m: number) => void }) {
  const options = [
    { m: 3, label: '3 months', est: 'about 40 seconds', note: 'enough for an active search' },
    { m: 12, label: '12 months', est: 'about 3 minutes', note: 'a full year of seasonality' },
    { m: 60, label: 'Everything', est: 'up to 20 minutes', note: 'capped at five years' },
  ];
  return (
    <>
      <div className="eyebrow">Step 4 of 7</div>
      <h1 className="headline" style={{ fontSize: 34, marginBottom: 'var(--space-3)' }}>
        How far back?
      </h1>
      <p className="muted-72" style={{ fontSize: 14, lineHeight: 1.6 }}>
        The first scan is the only slow part. It reads once, then only ever reads what is new.
      </p>
      <div style={{ display: 'grid', gap: 'var(--space-2)', marginTop: 'var(--space-6)' }}>
        {options.map((o) => (
          <button
            key={o.m}
            onClick={() => setMonths(o.m)}
            style={{
              textAlign: 'left',
              minHeight: 56,
              padding: 'var(--space-3)',
              border: '1px solid var(--color-divider)',
              background: months === o.m ? 'var(--color-accent-100)' : 'transparent',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between' }}>
              <strong style={{ font: '600 16px var(--font-heading)' }}>{o.label}</strong>
              <span className="muted-55" style={{ fontSize: 12 }}>{o.est}</span>
            </div>
            <div className="muted-60" style={{ fontSize: 12.5 }}>{o.note}</div>
          </button>
        ))}
      </div>
      <p className="muted-55" style={{ fontSize: 12, lineHeight: 1.5, marginTop: 'var(--space-4)' }}>
        Messages that are clearly not about a job are dropped before anything is stored — roughly 95% of
        what it reads.
      </p>
    </>
  );
}

function FirstScan() {
  const scan = useQuery<{ read: number; remaining: number }>({ queryKey: ['scan'], enabled: false });
  const health = useQuery({
    queryKey: ['mailboxes'],
    queryFn: () => api.get<{ backlog: number; placed_today: number }>('/api/mailboxes'),
    refetchInterval: 2_000,
  });
  const read = scan.data?.read ?? health.data?.placed_today ?? 0;
  const remaining = scan.data?.remaining ?? health.data?.backlog ?? 0;
  const total = read + remaining || 1;

  return (
    <>
      <div className="eyebrow">Step 5 of 7 · reading</div>
      <h1 className="headline" style={{ fontSize: 34, marginBottom: 'var(--space-4)' }}>
        Reading your mailbox
      </h1>
      <div className="bar-track" style={{ height: 9 }}>
        <div className="bar-fill" style={{ width: `${Math.round((read / total) * 100)}%` }} />
      </div>
      <div className="counters" style={{ marginTop: 'var(--space-4)' }}>
        <div>
          <div className="counter-n">{read}</div>
          <div className="counter-label">read</div>
        </div>
        <div className="counter-offer">
          <div className="counter-n">{health.data?.placed_today ?? 0}</div>
          <div className="counter-label">found</div>
        </div>
        <div>
          <div className="counter-n">{remaining}</div>
          <div className="counter-label">left</div>
        </div>
      </div>
      <p className="muted-55" style={{ fontSize: 12, lineHeight: 1.55, marginTop: 'var(--space-6)' }}>
        Showing what it finds while it works is the cheapest trust the product will ever buy. A spinner
        would have cost the same and earned nothing.
      </p>
    </>
  );
}

function ConfirmHaul() {
  const { data } = useQuery({
    queryKey: ['applications', 'haul'],
    queryFn: () => api.get<{ rows: Array<{ id: string; company: string; role: string; needs_review: boolean; applied_at: string | null }> }>('/api/applications?limit=200'),
  });
  const rows = data?.rows ?? [];

  return (
    <>
      <div className="eyebrow">Step 6 of 7</div>
      <h1 className="headline" style={{ fontSize: 34, marginBottom: 'var(--space-3)' }}>
        <span style={{ display: 'block' }}>Is this your</span>
        <span style={{ display: 'block' }}>history?</span>
      </h1>
      <p className="muted-72" style={{ fontSize: 14, lineHeight: 1.6 }}>
        {rows.length} found. Anything ambiguous is marked — those can wait.
      </p>
      <div style={{ marginTop: 'var(--space-4)' }}>
        {rows.map((r) => (
          <div
            key={r.id}
            style={{
              display: 'flex',
              gap: 'var(--space-3)',
              alignItems: 'center',
              padding: 'var(--space-3) 0',
              borderBottom: '1px solid var(--color-divider)',
            }}
          >
            <Mark />
            <span style={{ flex: 1 }}>
              <span style={{ display: 'block', fontSize: 14.5, fontWeight: 500 }}>{r.company}</span>
              <span className="muted-60" style={{ fontSize: 12.5 }}>
                {r.role}
                {r.applied_at ? ` · ${new Date(r.applied_at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })}` : ''}
              </span>
            </span>
            {r.needs_review ? (
              <span style={{ border: '1px solid var(--color-accent-400)', color: 'var(--color-accent-800)', fontSize: 11, padding: '1px 6px' }}>
                review
              </span>
            ) : null}
          </div>
        ))}
      </div>
      <p className="muted-55" style={{ fontSize: 12, lineHeight: 1.5, marginTop: 'var(--space-4)' }}>
        Every correction here is written back as a rule, so the same mistake does not survive to next
        month.
      </p>
    </>
  );
}

function Notifications() {
  const subscribe = useMutation({
    mutationFn: async () => {
      const { public_key: key } = await api.get<{ public_key: string | null }>('/api/push/key');
      if (!key) throw new Error('push is not configured on this box');
      const registration = await navigator.serviceWorker.ready;
      const sub = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key,
      });
      return api.post('/api/push/subscribe', sub.toJSON());
    },
  });

  return (
    <>
      <div className="eyebrow">Step 7 of 7</div>
      <h1 className="headline" style={{ fontSize: 34, marginBottom: 'var(--space-3)' }}>
        <span style={{ display: 'block' }}>One buzz</span>
        <span style={{ display: 'block' }}>a day, at most</span>
      </h1>
      <p className="muted-72" style={{ fontSize: 14, lineHeight: 1.6 }}>
        A job search is stressful enough. Loop is allowed to interrupt you for four things and nothing
        else.
      </p>
      <ul style={{ listStyle: 'none', padding: 0, margin: 'var(--space-6) 0 0', display: 'grid', gap: 'var(--space-3)' }}>
        {[
          'An interview is booked or moved',
          'A take-home deadline is near',
          'An offer or a decision arrives',
          'A thread has gone quiet past your own average',
        ].map((line) => (
          <li key={line} style={{ display: 'flex', gap: 'var(--space-3)', alignItems: 'center', fontSize: 14 }}>
            <Mark />
            {line}
          </li>
        ))}
      </ul>
      <div style={{ background: 'var(--color-accent-100)', padding: 'var(--space-4)', marginTop: 'var(--space-6)' }}>
        <div className="section-label" style={{ color: 'var(--color-accent-800)' }}>Fixed rules</div>
        <p style={{ fontSize: 12.5, lineHeight: 1.6, margin: 'var(--space-2) 0 0' }} className="muted-72">
          One notification a day · nothing between 21:00 and 08:00 · never twice for the same thing ·
          rejections are never pushed, they just appear in the log
        </p>
      </div>
      <Button
        style={{ width: '100%', marginTop: 'var(--space-4)' }}
        onClick={() => subscribe.mutate()}
        disabled={subscribe.isPending}
      >
        {subscribe.isSuccess ? 'Notifications on' : 'Turn notifications on'}
      </Button>
    </>
  );
}
