import { createPool, rewrapDek, withTransaction } from '@loop/db';

/**
 * `LOOP_KEK_OLD=… LOOP_KEK=… npm run rotate:kek`
 *
 * Re-wraps every DEK in one transaction. The runbook promises rotation works,
 * and an untested rotation is a promise rather than a procedure — so there is
 * also a test for `rewrapDek` in packages/db/src/db.itest.ts.
 */

const oldKek = Buffer.from(process.env.LOOP_KEK_OLD ?? '', 'base64');
const newKek = Buffer.from(process.env.LOOP_KEK ?? '', 'base64');

if (oldKek.length !== 32 || newKek.length !== 32) {
  console.error('set LOOP_KEK_OLD and LOOP_KEK to base64 32-byte keys');
  process.exit(1);
}
if (oldKek.equals(newKek)) {
  console.error('LOOP_KEK_OLD and LOOP_KEK are the same key; nothing to do');
  process.exit(1);
}

const pool = createPool({ applicationName: 'loop-rotate-kek' });

const rotated = await withTransaction(async (sql) => {
  const rows = await sql.query<{ id: string; dek_wrapped: Buffer; dek_nonce: Buffer }>(
    'select id, dek_wrapped, dek_nonce from mailbox_accounts for update',
  );
  for (const row of rows.rows) {
    const next = rewrapDek({ ciphertext: row.dek_wrapped, nonce: row.dek_nonce }, oldKek, newKek);
    await sql.query('update mailbox_accounts set dek_wrapped = $2, dek_nonce = $3 where id = $1', [
      row.id,
      next.ciphertext,
      next.nonce,
    ]);
  }
  return rows.rowCount ?? 0;
}, pool);

await pool.end();
console.log(`re-wrapped ${rotated} data key(s). Update LOOP_KEK in .env and restart.`);
