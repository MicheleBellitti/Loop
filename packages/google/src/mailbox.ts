import type pg from 'pg';
import { initCrypto, open, seal, unwrapDek, wrapDek, generateDek } from '@loop/db';
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
  cursor: { historyId?: string; syncToken?: string };
  watch_expires_at: Date | null;
  status: string;
  last_ok_at: Date | null;
}

export async function storeMailbox(
  sql: pg.PoolClient,
  input: {
    userId: string;
    provider: string;
    address: string;
    tokens: GoogleTokens;
  },
): Promise<string> {
  await initCrypto();
  const dek = generateDek();
  const wrapped = wrapDek(dek);
  const sealed = seal(JSON.stringify({ refresh_token: input.tokens.refresh_token }), dek);

  const res = await sql.query<{ id: string }>(
    `insert into mailbox_accounts
       (user_id, provider, address, secret_ciphertext, secret_nonce,
        dek_wrapped, dek_nonce, scopes, status)
     values ($1,$2,$3,$4,$5,$6,$7,$8,'ok')
     on conflict (user_id, provider, address) do update
       set secret_ciphertext = excluded.secret_ciphertext,
           secret_nonce      = excluded.secret_nonce,
           dek_wrapped       = excluded.dek_wrapped,
           dek_nonce         = excluded.dek_nonce,
           scopes            = excluded.scopes,
           status            = 'ok',
           last_error        = null
     returning id`,
    [
      input.userId,
      input.provider,
      input.address,
      sealed.ciphertext,
      sealed.nonce,
      wrapped.ciphertext,
      wrapped.nonce,
      input.tokens.scope.split(' '),
    ],
  );
  return res.rows[0]!.id;
}

export async function readRefreshToken(row: MailboxRow): Promise<string> {
  await initCrypto();
  const dek = unwrapDek({ ciphertext: row.dek_wrapped, nonce: row.dek_nonce });
  const plain = open({ ciphertext: row.secret_ciphertext, nonce: row.secret_nonce }, dek).toString('utf8');
  const parsed = JSON.parse(plain) as { refresh_token?: string };
  if (!parsed.refresh_token) throw new Error('mailbox has no refresh token');
  return parsed.refresh_token;
}

export async function markOk(sql: pg.PoolClient | pg.Pool, mailboxId: string): Promise<void> {
  await sql.query(
    `update mailbox_accounts set last_ok_at = now(), status = 'ok', last_error = null where id = $1`,
    [mailboxId],
  );
}

/**
 * Failure state F1 is the only full-screen failure in the product, because it
 * is the only one the system cannot fix alone. Setting this is what raises it.
 */
export async function markNeedsReauth(
  sql: pg.PoolClient | pg.Pool,
  mailboxId: string,
  reason: string,
): Promise<void> {
  await sql.query(
    `update mailbox_accounts set status = 'needs_reauth', last_error = $2 where id = $1`,
    [mailboxId, reason.slice(0, 500)],
  );
}

export async function markError(
  sql: pg.PoolClient | pg.Pool,
  mailboxId: string,
  reason: string,
): Promise<void> {
  await sql.query(`update mailbox_accounts set status = 'error', last_error = $2 where id = $1`, [
    mailboxId,
    reason.slice(0, 500),
  ]);
}

export async function saveCursor(
  sql: pg.PoolClient | pg.Pool,
  mailboxId: string,
  cursor: Record<string, unknown>,
): Promise<void> {
  await sql.query(`update mailbox_accounts set cursor = cursor || $2::jsonb where id = $1`, [
    mailboxId,
    JSON.stringify(cursor),
  ]);
}

export async function setBacklog(
  sql: pg.PoolClient | pg.Pool,
  mailboxId: string,
  n: number,
): Promise<void> {
  await sql.query(`update mailbox_accounts set backlog_estimate = $2 where id = $1`, [mailboxId, n]);
}
