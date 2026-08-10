import { CONNECTOR } from '@loop/domain';
export class GoogleAuthError extends Error {
    needsReauth;
    constructor(message, 
    /** `invalid_grant` means the user revoked access: failure state F1. */
    needsReauth) {
        super(message);
        this.needsReauth = needsReauth;
        this.name = 'GoogleAuthError';
    }
}
export class GoogleRateLimit extends Error {
    status;
    constructor(status) {
        super(`Google returned ${status}`);
        this.status = status;
        this.name = 'GoogleRateLimit';
    }
}
/** Gmail forgets history ids older than a week; a 404 means "re-list". */
export class HistoryTooOld extends Error {
}
export class GoogleClient {
    opts;
    apiBase;
    oauthBase;
    doFetch;
    constructor(opts) {
        this.opts = opts;
        this.apiBase = opts.apiBase ?? process.env.GOOGLE_API_BASE ?? 'https://www.googleapis.com';
        this.oauthBase = opts.oauthBase ?? process.env.GOOGLE_OAUTH_BASE ?? 'https://oauth2.googleapis.com';
        this.doFetch = opts.fetchImpl ?? fetch;
    }
    // ── OAuth ────────────────────────────────────────────────────────────────
    static authorizationUrl(params) {
        const url = new URL(process.env.GOOGLE_CONSENT_BASE ?? 'https://accounts.google.com/o/oauth2/v2/auth');
        url.searchParams.set('client_id', params.clientId);
        url.searchParams.set('redirect_uri', params.redirectUri);
        url.searchParams.set('response_type', 'code');
        // Read-only, and nothing else. No send, no modify, no contacts.
        url.searchParams.set('scope', 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/calendar.readonly');
        url.searchParams.set('access_type', 'offline');
        url.searchParams.set('prompt', 'consent');
        url.searchParams.set('code_challenge', params.codeChallenge);
        url.searchParams.set('code_challenge_method', 'S256');
        url.searchParams.set('state', params.state);
        if (params.loginHint)
            url.searchParams.set('login_hint', params.loginHint);
        return url.toString();
    }
    async exchangeCode(code, redirectUri, codeVerifier) {
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
        if (!res.ok)
            throw new GoogleAuthError(`token exchange failed: ${res.status}`, res.status === 400);
        return (await res.json());
    }
    async refresh(refreshToken) {
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
        return (await res.json());
    }
    async revoke(token) {
        await this.doFetch(`${this.oauthBase}/revoke`, {
            method: 'POST',
            headers: { 'content-type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ token }),
        }).catch(() => undefined);
    }
    // ── API ──────────────────────────────────────────────────────────────────
    async call(accessToken, path, init = {}) {
        const res = await this.withBackoff(() => this.doFetch(`${this.apiBase}${path}`, {
            ...init,
            headers: {
                ...(init.headers ?? {}),
                authorization: `Bearer ${accessToken}`,
                'content-type': 'application/json',
            },
        }));
        if (res.status === 404)
            throw new HistoryTooOld(path);
        if (res.status === 401 || res.status === 403) {
            throw new GoogleAuthError(`Google returned ${res.status} for ${path}`, true);
        }
        if (!res.ok)
            throw new Error(`Google returned ${res.status} for ${path}`);
        return (await res.json());
    }
    /** Exponential backoff, 1s → 64s, full jitter, eight attempts, then park. */
    async withBackoff(run) {
        let delay = CONNECTOR.BACKOFF_MIN_MS;
        for (let attempt = 1;; attempt++) {
            const res = await run();
            if (res.status !== 429 && res.status < 500)
                return res;
            if (attempt >= CONNECTOR.BACKOFF_ATTEMPTS)
                throw new GoogleRateLimit(res.status);
            const jittered = Math.random() * delay;
            await new Promise((r) => setTimeout(r, jittered));
            delay = Math.min(delay * 2, CONNECTOR.BACKOFF_MAX_MS);
        }
    }
    watch(accessToken, topic) {
        return this.call(accessToken, '/gmail/v1/users/me/watch', {
            method: 'POST',
            body: JSON.stringify({ topicName: topic, labelIds: ['INBOX'] }),
        });
    }
    stopWatch(accessToken) {
        return this.call(accessToken, '/gmail/v1/users/me/stop', { method: 'POST' });
    }
    profile(accessToken) {
        return this.call(accessToken, '/gmail/v1/users/me/profile');
    }
    history(accessToken, startHistoryId, pageToken) {
        const q = new URLSearchParams({ startHistoryId, historyTypes: 'messageAdded', labelId: 'INBOX' });
        if (pageToken)
            q.set('pageToken', pageToken);
        return this.call(accessToken, `/gmail/v1/users/me/history?${q}`);
    }
    listMessages(accessToken, query, pageToken, maxResults = CONNECTOR.BACKFILL_BATCH) {
        const q = new URLSearchParams({ q: query, maxResults: String(maxResults) });
        if (pageToken)
            q.set('pageToken', pageToken);
        return this.call(accessToken, `/gmail/v1/users/me/messages?${q}`);
    }
    getMessage(accessToken, id) {
        return this.call(accessToken, `/gmail/v1/users/me/messages/${id}?format=full`);
    }
    listCalendarEvents(accessToken, params) {
        const q = new URLSearchParams({ singleEvents: 'true', maxResults: '250' });
        if (params.syncToken)
            q.set('syncToken', params.syncToken);
        else if (params.timeMin)
            q.set('timeMin', params.timeMin);
        if (params.pageToken)
            q.set('pageToken', params.pageToken);
        return this.call(accessToken, `/calendar/v3/calendars/primary/events?${q}`);
    }
}
//# sourceMappingURL=google.js.map