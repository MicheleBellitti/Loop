/**
 * Follow-up drafts.
 *
 * "Template-based, in the thread's detected language, three lines maximum, no
 * adjectives about enthusiasm. Returned as text plus a `mailto:` deep link with
 * In-Reply-To set."
 *
 * And the sentence this whole module exists to honour: **there is no send
 * path**. The output is text and a link that opens the user's own mail client.
 * `npm run lint:no-send-path` asserts nothing in the repository can do more.
 */

export interface DraftInput {
  company: string;
  contactName: string | null;
  stageLabel: string;
  lastEventLabel: string | null;
  daysQuiet: number;
  language: 'it' | 'en' | 'other';
  threadMessageId: string | null;
  toAddress: string | null;
  subject: string | null;
}

export interface Draft {
  subject: string;
  body: string;
  mailto_url: string;
}

/**
 * Three lines: acknowledge, ask, close. No "I am very excited about this
 * opportunity" — a follow-up that grovels reads worse than one that does not,
 * and the product's job is to lower anxiety, not to perform it.
 */
function bodyEn(input: DraftInput): string {
  const context = input.lastEventLabel
    ? `thanks again for the ${input.lastEventLabel.toLowerCase()}.`
    : `thanks again for your time.`;
  return [
    `Hi${input.contactName ? ` ${input.contactName}` : ''},`,
    '',
    context,
    `Is there anything you need from my side while the team decides?`,
    '',
    'Best,',
  ].join('\n');
}

function bodyIt(input: DraftInput): string {
  const context = input.lastEventLabel
    ? `grazie ancora per ${input.lastEventLabel.toLowerCase()}.`
    : `grazie ancora per il tempo dedicato.`;
  return [
    `Ciao${input.contactName ? ` ${input.contactName}` : ''},`,
    '',
    context,
    `Serve qualcosa da parte mia mentre il team decide?`,
    '',
    'Un saluto,',
  ].join('\n');
}

export function buildDraft(input: DraftInput): Draft {
  const italian = input.language === 'it';
  const subject = input.subject
    ? input.subject.startsWith('Re:')
      ? input.subject
      : `Re: ${input.subject}`
    : italian
      ? `Re: candidatura ${input.company}`
      : `Re: ${input.company} application`;

  const body = italian ? bodyIt(input) : bodyEn(input);

  // The deep link carries In-Reply-To so the user's client threads the reply
  // rather than starting a new conversation.
  const params = new URLSearchParams({ subject, body });
  if (input.threadMessageId) params.set('In-Reply-To', `<${input.threadMessageId.replace(/^<|>$/g, '')}>`);
  const mailto = `mailto:${input.toAddress ?? ''}?${params.toString()}`;

  return { subject, body, mailto_url: mailto };
}
