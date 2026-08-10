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
export declare class GoogleAuthError extends Error {
    /** `invalid_grant` means the user revoked access: failure state F1. */
    readonly needsReauth: boolean;
    constructor(message: string, 
    /** `invalid_grant` means the user revoked access: failure state F1. */
    needsReauth: boolean);
}
export declare class GoogleRateLimit extends Error {
    readonly status: number;
    constructor(status: number);
}
/** Gmail forgets history ids older than a week; a 404 means "re-list". */
export declare class HistoryTooOld extends Error {
}
export interface GoogleClientOptions {
    apiBase?: string;
    oauthBase?: string;
    clientId: string;
    clientSecret: string;
    fetchImpl?: typeof fetch;
}
export declare class GoogleClient {
    private readonly opts;
    private readonly apiBase;
    private readonly oauthBase;
    private readonly doFetch;
    constructor(opts: GoogleClientOptions);
    static authorizationUrl(params: {
        clientId: string;
        redirectUri: string;
        codeChallenge: string;
        state: string;
        loginHint?: string;
    }): string;
    exchangeCode(code: string, redirectUri: string, codeVerifier: string): Promise<GoogleTokens>;
    refresh(refreshToken: string): Promise<GoogleTokens>;
    revoke(token: string): Promise<void>;
    private call;
    /** Exponential backoff, 1s → 64s, full jitter, eight attempts, then park. */
    private withBackoff;
    watch(accessToken: string, topic: string): Promise<{
        historyId: string;
        expiration: string;
    }>;
    stopWatch(accessToken: string): Promise<unknown>;
    profile(accessToken: string): Promise<{
        emailAddress: string;
        historyId: string;
    }>;
    history(accessToken: string, startHistoryId: string, pageToken?: string): Promise<{
        history?: Array<{
            messagesAdded?: Array<{
                message: {
                    id: string;
                };
            }>;
        }>;
        historyId?: string;
        nextPageToken?: string;
    }>;
    listMessages(accessToken: string, query: string, pageToken?: string, maxResults?: 250): Promise<{
        messages?: Array<{
            id: string;
            threadId: string;
        }>;
        nextPageToken?: string;
    }>;
    getMessage(accessToken: string, id: string): Promise<GmailMessage>;
    listCalendarEvents(accessToken: string, params: {
        syncToken?: string;
        timeMin?: string;
        pageToken?: string;
    }): Promise<CalendarListResponse>;
}
export interface GmailMessagePart {
    mimeType?: string;
    filename?: string;
    headers?: Array<{
        name: string;
        value: string;
    }>;
    body?: {
        data?: string;
        size?: number;
        attachmentId?: string;
    };
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
    start?: {
        dateTime?: string;
        date?: string;
    };
    end?: {
        dateTime?: string;
        date?: string;
    };
    organizer?: {
        email?: string;
    };
    attendees?: Array<{
        email?: string;
    }>;
}
export interface CalendarListResponse {
    items?: CalendarEvent[];
    nextPageToken?: string;
    nextSyncToken?: string;
}
//# sourceMappingURL=google.d.ts.map