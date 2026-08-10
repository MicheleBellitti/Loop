import { mkdir, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The synthetic corpus.
 *
 * §17 wants 150 real messages, anonymised, plus 100 negatives — and it is right
 * that the real corpus is the project's spine. This generates the *structural*
 * corpus that CI can run on a clean checkout: every ATS vendor, both languages,
 * each intent, and the negatives that must be dropped. It is what makes the
 * confusion matrix reproducible for someone who has never seen your inbox.
 *
 * Your own mail is added on top with `scripts/anonymise.ts`, into
 * `fixtures/private/` (git-ignored). The gate that decides whether the product
 * ships — ≥0.85 application-level recall over twelve real months — can only be
 * measured there.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = join(HERE, '..');

interface Fixture {
  name: string;
  expect: {
    intent?: string;
    company?: string;
    vendor?: string;
    drop?: boolean;
    /**
     * Set when only the model can place this message. With rung 3 off — the
     * default posture — these land in the review queue instead, which is
     * failure state F4 and is a correct outcome, not a miss.
     */
    requires_model?: boolean;
  };
  from: string;
  to?: string;
  subject: string;
  headers?: Record<string, string>;
  body: string;
  html?: boolean;
  ics?: string;
}

const eml = (f: Fixture): string => {
  const lines = [
    `From: ${f.from}`,
    `To: ${f.to ?? 'you@example.com'}`,
    `Subject: ${f.subject}`,
    `Date: Thu, 30 Jul 2026 09:12:00 +0200`,
    `Message-ID: <${f.name}@fixture.loop>`,
    ...Object.entries(f.headers ?? {}).map(([k, v]) => `${k}: ${v}`),
  ];

  if (f.ics) {
    lines.push('MIME-Version: 1.0', 'Content-Type: multipart/mixed; boundary="b1"', '', '--b1',
      'Content-Type: text/plain; charset=utf-8', '', f.body, '', '--b1',
      'Content-Type: text/calendar; method=REQUEST; charset=utf-8',
      'Content-Disposition: attachment; filename="invite.ics"', '', f.ics, '', '--b1--');
  } else {
    lines.push(`Content-Type: text/${f.html ? 'html' : 'plain'}; charset=utf-8`, '', f.body);
  }
  return `${lines.join('\n')}\n`;
};

const ics = (uid: string, summary: string, start: string): string =>
  [
    'BEGIN:VCALENDAR', 'VERSION:2.0', 'METHOD:REQUEST', 'BEGIN:VEVENT',
    `UID:${uid}`, `SUMMARY:${summary}`, `DTSTART:${start}`, `DTEND:${start.replace(/T(\d{2})/, (_m, h) => `T${String(Number(h) + 1).padStart(2, '0')}`)}`,
    'ORGANIZER:mailto:talent@nexi.it', 'ATTENDEE:mailto:you@example.com',
    'STATUS:CONFIRMED', 'END:VEVENT', 'END:VCALENDAR',
  ].join('\r\n');

const ATS: Fixture[] = [
  {
    name: 'greenhouse-ack-01',
    expect: { intent: 'acknowledged', company: 'Zalando', vendor: 'greenhouse' },
    from: 'no-reply@eu.greenhouse-mail.io',
    subject: 'Thank you for applying to Zalando',
    body: 'Hello,\n\nWe have received your application for the Senior Backend Engineer position and the team will review it shortly.\n\nThe Zalando Talent Team',
  },
  {
    name: 'greenhouse-reject-01',
    expect: { intent: 'rejected', vendor: 'greenhouse' },
    from: 'no-reply@greenhouse-mail.io',
    subject: 'Update on your application',
    body: 'Thank you for your interest. After careful review we have decided to move forward with other candidates for this role.',
  },
  {
    name: 'greenhouse-screening-01',
    expect: { intent: 'schedule_screening', company: 'Personio', vendor: 'greenhouse' },
    from: 'no-reply@greenhouse-mail.io',
    subject: 'Personio | Interview availability',
    body: 'Hi,\n\nWe would like to speak with you for the Backend Engineer position. Please share your availability for the coming week.',
  },
  {
    name: 'lever-ack-01',
    expect: { intent: 'acknowledged', company: 'Docebo', vendor: 'lever' },
    from: 'no-reply@hire.lever.co',
    subject: 'Docebo has received your application',
    body: 'Thanks for applying to Docebo. Our team reviews every application.',
  },
  {
    name: 'workday-ack-01',
    expect: { intent: 'acknowledged', company: 'Nexi', vendor: 'workday' },
    from: 'nexi@myworkday.com',
    subject: 'Nexi: Application Received',
    body: 'Dear candidate, thank you for your interest in the Platform Engineer position at Nexi.',
  },
  {
    name: 'workday-reject-it-01',
    expect: { intent: 'rejected', vendor: 'workday' },
    from: 'nexi@myworkday.com',
    subject: 'Aggiornamento sulla tua candidatura',
    body: 'Gentile candidato, la ringraziamo per il tempo dedicato. Abbiamo deciso di proseguire con altri profili per questa posizione.',
  },
  {
    name: 'ashby-ack-01',
    expect: { intent: 'acknowledged', company: 'Satispay', vendor: 'ashby' },
    from: 'notifications@ashbyhq.com',
    subject: 'Satispay — Application Received',
    body: 'We have received your application for Backend Engineer.',
  },
  {
    name: 'ashby-takehome-01',
    expect: { intent: 'take_home', vendor: 'ashby' },
    from: 'notifications@ashbyhq.com',
    subject: 'Next step at Satispay',
    body: 'The next step is a take-home exercise. Please submit your solution by Sunday 3 August.',
  },
  {
    name: 'smartrecruiters-ack-01',
    expect: { intent: 'acknowledged', company: 'Sportradar', vendor: 'smartrecruiters' },
    from: 'no-reply@smartrecruiters.com',
    subject: 'Thank you for applying at Sportradar',
    body: 'Your application has been received.',
  },
  {
    name: 'workable-ack-01',
    expect: { intent: 'acknowledged', company: 'Prima Assicurazioni', vendor: 'workable' },
    from: 'no-reply@workablemail.com',
    subject: 'Thank you for applying to Prima Assicurazioni',
    body: 'We have your application for Backend Engineer.',
  },
  {
    name: 'icims-ack-01',
    expect: { intent: 'acknowledged', vendor: 'icims' },
    from: 'careers@talent.icims.com',
    subject: 'Thank you for your interest in our company',
    body: 'Your application has been submitted successfully.',
  },
  {
    name: 'taleo-ack-01',
    expect: { intent: 'acknowledged', company: 'Iliad Italia', vendor: 'taleo' },
    from: 'noreply@taleo.net',
    subject: 'Candidatura ricevuta - Iliad Italia',
    body: 'Abbiamo ricevuto la sua candidatura per la posizione di Software Engineer.',
  },
  {
    name: 'recruitee-ack-01',
    expect: { intent: 'acknowledged', company: 'Translated', vendor: 'recruitee' },
    from: 'no-reply@mail.recruitee.com',
    subject: 'Translated - Application received',
    body: 'Thanks for applying at Translated.',
  },
  {
    name: 'bamboohr-ack-01',
    expect: { intent: 'acknowledged', vendor: 'bamboohr' },
    from: 'no-reply@mail.bamboohr.com',
    subject: 'Everli Application Confirmation',
    body: 'Thank you for your application.',
  },
  // The two that arrive bulk-flagged. "This is the single most common
  // false-negative in the whole system; there is a fixture for it."
  {
    name: 'linkedin-applied-01',
    expect: { intent: 'applied', company: 'Nexi', vendor: 'linkedin' },
    from: 'jobs-noreply@linkedin.com',
    subject: 'Your application was sent to Nexi',
    headers: {
      'List-Unsubscribe': '<https://www.linkedin.com/unsubscribe>',
      'List-Id': 'jobs.linkedin.com',
      Precedence: 'bulk',
    },
    body: 'Your application for Platform Engineer was sent to Nexi.',
  },
  {
    name: 'linkedin-applied-it-01',
    expect: { intent: 'applied', company: 'Casavo', vendor: 'linkedin' },
    from: 'jobs-noreply@linkedin.com',
    subject: 'La tua candidatura è stata inviata a Casavo',
    headers: { 'List-Unsubscribe': '<https://www.linkedin.com/unsubscribe>', Precedence: 'bulk' },
    body: 'La tua candidatura per Senior Engineer è stata inviata a Casavo.',
  },
  {
    name: 'indeed-applied-01',
    expect: { intent: 'applied', company: 'Everli', vendor: 'indeed' },
    from: 'noreply@indeedemail.com',
    subject: 'Indeed Application: Backend Engineer - Everli',
    headers: { 'List-Id': 'indeed-apply', Precedence: 'bulk' },
    body: 'You applied to Backend Engineer at Everli.',
  },
  // Rung 2: the calendar path.
  {
    name: 'calendar-invite-01',
    expect: { intent: 'interview_invite' },
    from: 'Marta <talent@nexi.it>',
    subject: 'Invitation: System & code review @ Fri 31 Jul',
    body: 'Looking forward to it.',
    ics: ics('nexi-round-2@nexi.it', 'System & code review', '20260731T100000Z'),
  },
  {
    name: 'calendar-invite-hr-01',
    expect: { intent: 'interview_invite' },
    from: 'People <people@satispay.com>',
    subject: 'Invitation: Intro call',
    body: 'A short introductory call.',
    ics: ics('satispay-intro@satispay.com', 'HR screening call', '20260801T090000Z'),
  },
  // Rung 3 territory: a human wrote it, no template matches.
  {
    name: 'human-offer-01',
    expect: { intent: 'offer', requires_model: true },
    from: 'Giulia <giulia@bendingspoons.com>',
    subject: 'Offer — Bending Spoons',
    body: 'Hi,\n\nWe would like to offer you the Software Engineer role at €68,000 base plus equity. Could you let us know by 8 August?\n\nGiulia',
  },
  {
    name: 'human-italian-reject-01',
    expect: { intent: 'rejected', requires_model: true },
    from: 'hr@iliad.it',
    subject: 'La tua candidatura',
    body: 'Ti terremo in considerazione per future opportunità. Grazie ancora per il tempo dedicato.',
  },
];

const NEGATIVES: Fixture[] = [
  {
    name: 'newsletter-01',
    expect: { drop: true },
    from: 'news@techweekly.example.com',
    subject: 'This week in engineering',
    headers: { 'List-Id': 'techweekly', 'List-Unsubscribe': '<https://example.com/u>', Precedence: 'bulk' },
    body: 'The five best articles about distributed systems this week.',
  },
  {
    name: 'github-01',
    expect: { drop: true },
    from: 'notifications@github.com',
    subject: '[acme/repo] Pull request merged (#412)',
    headers: { 'List-Id': 'acme/repo', Precedence: 'bulk' },
    body: 'The pull request was merged into main.',
  },
  {
    name: 'social-01',
    expect: { drop: true },
    from: 'notification@facebookmail.com',
    subject: 'You have 3 new notifications',
    headers: { 'List-Unsubscribe': '<https://facebook.com/u>', Precedence: 'bulk' },
    body: 'See what your friends have been posting.',
  },
  {
    name: 'invoice-01',
    expect: { drop: true },
    from: 'billing@hosting.example.com',
    subject: 'Your invoice for July',
    headers: { Precedence: 'bulk', 'List-Id': 'billing' },
    body: 'Your invoice is attached. No action is required.',
  },
  {
    name: 'linkedin-jobalert-01',
    expect: { drop: false },
    from: 'jobalerts-noreply@linkedin.com',
    subject: '12 new jobs for Backend Engineer',
    headers: { 'List-Id': 'jobs.linkedin.com', Precedence: 'bulk' },
    // Not dropped by the classifier — the LinkedIn whitelist keeps it — but
    // rung 1 finds no pattern, so it abstains and the message is discarded
    // later. That is the correct division of labour, and this fixture pins it.
    body: 'Here are jobs that match your search.',
  },
  {
    name: 'calendar-personal-01',
    expect: { drop: true },
    from: 'mum@gmail.com',
    subject: 'Dinner Sunday',
    body: 'Are you free on Sunday evening?',
  },
  {
    name: 'marketing-01',
    expect: { drop: true },
    from: 'offers@shop.example.com',
    subject: 'Summer sale — 40% off everything',
    headers: { Precedence: 'bulk', 'List-Unsubscribe': '<https://shop.example.com/u>' },
    body: 'Shop the sale before it ends.',
  },
  {
    name: 'security-alert-01',
    expect: { drop: true },
    from: 'no-reply@accounts.google.com',
    subject: 'Security alert',
    body: 'A new device signed in to your account.',
  },
];

async function main(): Promise<void> {
  await mkdir(join(ROOT, 'fixtures', 'ats'), { recursive: true });
  await mkdir(join(ROOT, 'fixtures', 'negatives'), { recursive: true });

  const manifest: Array<{ file: string; expect: Fixture['expect'] }> = [];

  for (const f of ATS) {
    const file = `fixtures/ats/${f.name}.eml`;
    await writeFile(join(ROOT, file), eml(f), 'utf8');
    manifest.push({ file, expect: f.expect });
  }
  for (const f of NEGATIVES) {
    const file = `fixtures/negatives/${f.name}.eml`;
    await writeFile(join(ROOT, file), eml(f), 'utf8');
    manifest.push({ file, expect: f.expect });
  }

  await writeFile(join(ROOT, 'fixtures', 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8');
  console.log(`wrote ${ATS.length} ATS fixtures and ${NEGATIVES.length} negatives`);
}

await main();
