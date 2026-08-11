import { CONNECTOR } from '@loop/domain';

/**
 * A thin Google client over `fetch`.
 *
 * §02 names `googleapis`. This is a hand-rolled client instead, and the reason
 * is the test strategy the spec itself asks for: "a stub OAuth/Gmail server
 * that replays fixture messages through the real connector path". A client
 * whose base URL is one environment variable makes that stub trivial and keeps
 * the *real* code under test; the official SDK bakes its hosts in and would
 * force the tests to mock the library instead of the protocol — which tests the
 * mock. Six endpoints are used in total. decisions.md B17.
 */

export interface GoogleTokens {
  access_token: string;
  refresh_token?: string;
  expires_in: number;
  scope: string;
  token_type: string;
}

export class GoogleAuthError extends Error {
  constructor(
    message: string,
    /** `invalid_grant` means the user revoked access: failure state F1. */
    readonly needsReauth: boolean,
  ) {
    super(message);
    this.name = 'GoogleAuthError';
  }
}

export class GoogleRateLimit extends Error {
  constructor(readonly status: number) {
    super(`Google returned ${status}`);
    this.name = 'GoogleRateLimit';
  }
}

/**
 * Google's own words, trimmed to one line and capped.
 *
 * Never the whole body: an error response can echo request content, and this
 * string goes to a log that must stay safe to keep (§16 — "never subject
 * lines, never sender addresses, never body fragments").
 */
async function readGoogleError(res: Response): Promise<string> {
  try {
    const text = (await res.text()).slice(0, 2_000);
    try {
      const body = JSON.parse(text) as { error?: { message?: string; status?: string } };
      const msg = body.error?.message ?? '';
      const status = body.error?.status ?? '';
      return [status, msg].filter(Boolean).join(' — ').replace(/\s+/g, ' ').slice(0, 300);
    } catch {
      return text.replace(/\s+/g, ' ').slice(0, 300);
    }
  } catch {
    return '';
  }
}

/** Gmail forgets history ids older than a week; a 404 means "re-list". */
export class HistoryTooOld extends Error {}

export interface GoogleClientOptions {
  apiBase?: string;
  oauthBase?: string;
  clientId: string;
  clientSecret: string;
  fetchImpl?: typeof fetch;
}

export class GoogleClient {
  private readonly apiBase: string;
  private readonly oauthBase: string;
  private readonly doFetch: typeof fetch;

  constructor(private readonly opts: GoogleClientOptions) {
    this.apiBase = opts.apiBase ?? process.env.GOOGLE_API_BASE ?? 'https://www.googleapis.com';
    this.oauthBase = opts.oauthBase ?? process.env.GOOGLE_OAUTH_BASE ?? 'https://oauth2.googleapis.com';
    this.doFetch = opts.fetchImpl ?? fetch;
  }

  // ── OAuth ────────────────────────────────────────────────────────────────

  static authorizationUrl(params: {
    clientId: string;
    redirectUri: string;
    codeChallenge: string;
    state: string;
    loginHint?: string;
  }): string {
    const url = new URL(
      process.env.GOOGLE_CONSENT_BASE ?? 'https://accounts.google.com/o/oauth2/v2/auth',
    );
    url.searchParams.set('client_id', params.clientId);
    url.searchParams.set('redirect_uri', params.redirectUri);
    url.searchParams.set('response_type', 'code');
    // Read-only, and nothing else. No send, no modify, no contacts.
    url.searchParams.set(
      'scope',
      'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly',
    );
    url.searchParams.set('access_type', 'offline');
    url.searchParams.set('prompt', 'consent');
    url.searchParams.set('code_challenge', params.codeChallenge);
    url.searchParams.set('code_challenge_method', 'S256');
    url.searchParams.set('state', params.state);
    if (params.loginHint) url.searchParams.set('login_hint', params.loginHint);
    return url.toString();
  }

  async exchangeCode(code: string, redirectUri: string, codeVerifier: string): Promise<GoogleTokens> {
    const res = await this.doFetch(`${this.oauthBase}/token`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        code,
        client_id: this.opts.clientId,
        client_secret: this.opts.clientSecret,
        redirect_uri: redirectUri,
        grant_type: 'authorization_code',
        code_verifier: codeVerifier,
      }),
    });
    if (!res.ok) throw new GoogleAuthError(`token exchange failed: ${res.status}`, res.status === 400);
    return (await res.json()) as GoogleTokens;
  }

  async refresh(refreshToken: string): Promise<GoogleTokens> {
    const res = await this.doFetch(`${this.oauthBase}/token`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        refresh_token: refreshToken,
        client_id: this.opts.clientId,
        client_secret: this.opts.clientSecret,
        grant_type: 'refresh_token',
      }),
    });
    if (!res.ok) {
      const body = await res.text();
      // 401/403 invalid_grant → the grant is gone. Stop, surface F1, and emit
      // no further work: retrying a revoked grant only burns quota.
      throw new GoogleAuthError(`refresh failed: ${res.status} ${body}`, /invalid_grant/.test(body) || res.status === 401);
    }
    return (await res.json()) as GoogleTokens;
  }

  async revoke(token: string): Promise<void> {
    await this.doFetch(`${this.oauthBase}/revoke`, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ token }),
    }).catch(() => undefined);
  }

  // ── API ──────────────────────────────────────────────────────────────────

  private async call<T>(accessToken: string, path: string, init: RequestInit = {}): Promise<T> {
    const res = await this.withBackoff(() =>
      this.doFetch(`${this.apiBase}${path}`, {
        ...init,
        headers: {
          ...(init.headers ?? {}),
          authorization: `Bearer ${accessToken}`,
          'content-type': 'application/json',
        },
      }),
    );
    if (res.status === 404) throw new HistoryTooOld(path);
    if (!res.ok) {
      // Google explains itself in the body — "Gmail API has not been used in
      // project N before or it is disabled", "Request had insufficient
      // authentication scopes". Throwing away that sentence turns a five-second
      // fix into a guess, which is the opposite of what §16 asks of an error.
      const reason = await readGoogleError(res);
      const where = `${res.status} for ${path}${reason ? `: ${reason}` : ''}`;
      if (res.status === 401 || res.status === 403) {
        throw new GoogleAuthError(`Google returned ${where}`, true);
      }
      throw new Error(`Google returned ${where}`);
    }
    return (await res.json()) as T;
  }

  /** Exponential backoff, 1s → 64s, full jitter, eight attempts, then park. */
  private async withBackoff(run: () => Promise<Response>): Promise<Response> {
    let delay: number = CONNECTOR.BACKOFF_MIN_MS;
    for (let attempt = 1; ; attempt++) {
      const res = await run();
      if (res.status !== 429 && res.status < 500) return res;
      if (attempt >= CONNECTOR.BACKOFF_ATTEMPTS) throw new GoogleRateLimit(res.status);
      const jittered = Math.random() * delay;
      await new Promise((r) => setTimeout(r, jittered));
      delay = Math.min(delay * 2, CONNECTOR.BACKOFF_MAX_MS);
    }
  }

  watch(accessToken: string, topic: string): Promise<{ historyId: string; expiration: string }> {
    return this.call(accessToken, '/gmail/v1/users/me/watch', {
      method: 'POST',
      body: JSON.stringify({ topicName: topic, labelIds: ['INBOX'] }),
    });
  }

  stopWatch(accessToken: string): Promise<unknown> {
    return this.call(accessToken, '/gmail/v1/users/me/stop', { method: 'POST' });
  }

  profile(accessToken: string): Promise<{ emailAddress: string; historyId: string }> {
    return this.call(accessToken, '/gmail/v1/users/me/profile');
  }

  history(
    accessToken: string,
    startHistoryId: string,
    pageToken?: string,
  ): Promise<{ history?: Array<{ messagesAdded?: Array<{ message: { id: string } }> }>; historyId?: string; nextPageToken?: string }> {
    const q = new URLSearchParams({ startHistoryId, historyTypes: 'messageAdded', labelId: 'INBOX' });
    if (pageToken) q.set('pageToken', pageToken);
    return this.call(accessToken, `/gmail/v1/users/me/history?${q}`);
  }

  listMessages(
    accessToken: string,
    query: string,
    pageToken?: string,
    maxResults = CONNECTOR.BACKFILL_BATCH,
  ): Promise<{ messages?: Array<{ id: string; threadId: string }>; nextPageToken?: string }> {
    const q = new URLSearchParams({ q: query, maxResults: String(maxResults) });
    if (pageToken) q.set('pageToken', pageToken);
    return this.call(accessToken, `/gmail/v1/users/me/messages?${q}`);
  }

  getMessage(accessToken: string, id: string): Promise<GmailMessage> {
    return this.call(accessToken, `/gmail/v1/users/me/messages/${id}?format=full`);
  }

  /**
   * Fetch an attachment body.
   *
   * `format=full` inlines small parts as base64 in `body.data`, but anything
   * past a few kilobytes comes back as a bare `attachmentId` with an empty
   * body. A real Google Calendar invitation is one of those — which meant every
   * `.ics` in this mailbox parsed as null, and the single cheapest, most
   * certain stage detector in the whole design never fired once.
   */
  getAttachment(accessToken: string, messageId: string, attachmentId: string): Promise<{ data: string; size: number }> {
    return this.call(
      accessToken,
      `/gmail/v1/users/me/messages/${messageId}/attachments/${attachmentId}`,
    );
  }

  /**
   * Return the message with any calendar part's body filled in.
   *
   * Done here rather than in the normaliser so that `toRawMessage` stays a pure
   * function of a message — the fetch is I/O and belongs on this side.
   */
  async hydrateCalendarParts(accessToken: string, msg: GmailMessage): Promise<GmailMessage> {
    const parts: GmailMessagePart[] = [];
    const walk = (p: GmailMessagePart | undefined): void => {
      if (!p) return;
      parts.push(p);
      for (const child of p.parts ?? []) walk(child);
    };
    walk(msg.payload);

    const needed = parts.filter(
      (p) =>
        (p.mimeType === 'text/calendar' || /\.ics$/i.test(p.filename ?? '')) &&
        !p.body?.data &&
        p.body?.attachmentId,
    );
    for (const part of needed) {
      try {
        const att = await this.getAttachment(accessToken, msg.id, part.body!.attachmentId!);
        part.body!.data = att.data;
      } catch {
        // An attachment can be gone; the message is still worth extracting.
      }
    }
    return msg;
  }

  listCalendarEvents(
    accessToken: string,
    params: { syncToken?: string; timeMin?: string; pageToken?: string },
  ): Promise<CalendarListResponse> {
    const q = new URLSearchParams({ singleEvents: 'true', maxResults: '250' });
    if (params.syncToken) q.set('syncToken', params.syncToken);
    else if (params.timeMin) q.set('timeMin', params.timeMin);
    if (params.pageToken) q.set('pageToken', params.pageToken);
    return this.call(accessToken, `/calendar/v3/calendars/primary/events?${q}`);
  }
}

export interface GmailMessagePart {
  mimeType?: string;
  filename?: string;
  headers?: Array<{ name: string; value: string }>;
  body?: { data?: string; size?: number; attachmentId?: string };
  parts?: GmailMessagePart[];
}

export interface GmailMessage {
  id: string;
  threadId: string;
  internalDate?: string;
  labelIds?: string[];
  payload?: GmailMessagePart;
  snippet?: string;
}

export interface CalendarEvent {
  id: string;
  iCalUID?: string;
  status?: string;
  summary?: string;
  location?: string;
  start?: { dateTime?: string; date?: string };
  end?: { dateTime?: string; date?: string };
  organizer?: { email?: string };
  attendees?: Array<{ email?: string }>;
}

export interface CalendarListResponse {
  items?: CalendarEvent[];
  nextPageToken?: string;
  nextSyncToken?: string;
}
