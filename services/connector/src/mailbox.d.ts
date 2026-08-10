import type pg from 'pg';
import type { GoogleTokens } from './google.js';
/**
 * Mailbox secrets, sealed.
 *
 * "Plaintext secrets exist only inside the connector process, only for the
 * length of one call, and are never placed in a variable that a logger can
 * reach." Everything that returns a token here returns it to a caller that uses
 * it immediately and lets it fall out of scope.
 */
export interface MailboxRow {
    id: string;
    user_id: string;
    provider: string;
    address: string;
    secret_ciphertext: Buffer;
    secret_nonce: Buffer;
    dek_wrapped: Buffer;
    dek_nonce: Buffer;
    scopes: string[];
    cursor: {
        historyId?: string;
        syncToken?: string;
    };
    watch_expires_at: Date | null;
    status: string;
    last_ok_at: Date | null;
}
export declare function storeMailbox(sql: pg.PoolClient, input: {
    userId: string;
    provider: string;
    address: string;
    tokens: GoogleTokens;
}): Promise<string>;
export declare function readRefreshToken(row: MailboxRow): Promise<string>;
export declare function markOk(sql: pg.PoolClient | pg.Pool, mailboxId: string): Promise<void>;
/**
 * Failure state F1 is the only full-screen failure in the product, because it
 * is the only one the system cannot fix alone. Setting this is what raises it.
 */
export declare function markNeedsReauth(sql: pg.PoolClient | pg.Pool, mailboxId: string, reason: string): Promise<void>;
export declare function markError(sql: pg.PoolClient | pg.Pool, mailboxId: string, reason: string): Promise<void>;
export declare function saveCursor(sql: pg.PoolClient | pg.Pool, mailboxId: string, cursor: Record<string, unknown>): Promise<void>;
export declare function setBacklog(sql: pg.PoolClient | pg.Pool, mailboxId: string, n: number): Promise<void>;
//# sourceMappingURL=mailbox.d.ts.map