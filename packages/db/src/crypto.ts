import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto';

/**
 * Envelope encryption for mailbox secrets.
 *
 * Per-user data key wrapping the refresh token; DEKs wrapped by a KEK held
 * outside the database. A stolen dump yields nothing readable, which is the
 * only claim worth making about a box that reads your mail.
 *
 * ── Why not libsodium ──────────────────────────────────────────────────────
 *
 * §02 names `sodium-native` for `crypto_secretbox`, and its reason is the right
 * one: "audited primitives, no room to choose a mode wrongly." That reason is
 * satisfied here by AES-256-GCM from Node's own crypto — an audited AEAD, one
 * mode, no options to get wrong, and no dependency at all. The alternatives
 * both cost something this deployment cannot afford: `sodium-native` is a
 * native addon that needs a compiler in the image and a rebuild on every Node
 * bump on an ARM box, and `libsodium-wrappers` ships an ESM build whose
 * internal import does not resolve under Node's resolver.
 *
 * The one real difference is nonce size: XSalsa20's 24 random bytes tolerate
 * far more messages per key than GCM's 12 before birthday-bound collisions
 * matter. It does not bite here — a DEK seals one secret per mailbox, and a KEK
 * wraps one DEK per user, so the count is in the single digits against a bound
 * of 2^32. If that ever stops being true, the fix is a fresh DEK per message,
 * not a bigger nonce. decisions.md B18.
 */

const ALGORITHM = 'aes-256-gcm';
const NONCE_BYTES = 12;
const KEY_BYTES = 32;
const TAG_BYTES = 16;

export interface Sealed {
  /** Ciphertext with the 16-byte auth tag appended. */
  ciphertext: Buffer;
  nonce: Buffer;
}

/** Kept for API symmetry with the async libsodium version the spec assumed. */
export async function initCrypto(): Promise<void> {
  // Node's crypto needs no initialisation; this exists so callers written
  // against the spec's `await initCrypto()` keep working.
}

export function loadKek(env = process.env.LOOP_KEK): Buffer {
  if (!env) {
    throw new Error(
      'LOOP_KEK is not set. Generate one with: node -e "console.log(require(\'crypto\').randomBytes(32).toString(\'base64\'))"',
    );
  }
  const key = Buffer.from(env, 'base64');
  if (key.length !== KEY_BYTES) throw new Error(`LOOP_KEK must decode to 32 bytes, got ${key.length}`);
  return key;
}

export function generateDek(): Buffer {
  return randomBytes(KEY_BYTES);
}

export function seal(plaintext: Buffer | string, key: Buffer): Sealed {
  if (key.length !== KEY_BYTES) throw new Error('key must be 32 bytes');
  const nonce = randomBytes(NONCE_BYTES);
  const cipher = createCipheriv(ALGORITHM, key, nonce);
  const body = Buffer.concat([
    cipher.update(typeof plaintext === 'string' ? Buffer.from(plaintext, 'utf8') : plaintext),
    cipher.final(),
  ]);
  return { ciphertext: Buffer.concat([body, cipher.getAuthTag()]), nonce };
}

export function open(sealed: Sealed, key: Buffer): Buffer {
  if (key.length !== KEY_BYTES) throw new Error('key must be 32 bytes');
  if (sealed.ciphertext.length < TAG_BYTES) throw new Error('ciphertext is too short to carry a tag');

  const body = sealed.ciphertext.subarray(0, sealed.ciphertext.length - TAG_BYTES);
  const tag = sealed.ciphertext.subarray(sealed.ciphertext.length - TAG_BYTES);
  const decipher = createDecipheriv(ALGORITHM, key, sealed.nonce);
  decipher.setAuthTag(tag);
  // A wrong key throws here rather than returning plausible bytes, which is the
  // whole reason this is an AEAD and not a raw cipher.
  return Buffer.concat([decipher.update(body), decipher.final()]);
}

export function openUtf8(sealed: Sealed, key: Buffer): string {
  return open(sealed, key).toString('utf8');
}

/** DEK sealed with the KEK. What goes in `mailbox_accounts.dek_wrapped`. */
export function wrapDek(dek: Buffer, kek: Buffer = loadKek()): Sealed {
  return seal(dek, kek);
}

export function unwrapDek(wrapped: Sealed, kek: Buffer = loadKek()): Buffer {
  return open(wrapped, kek);
}

/**
 * KEK rotation: re-wrap every DEK in one transaction. `scripts/rotate-kek.ts`
 * drives it and a test covers it — because the runbook promises it works, and
 * an untested rotation is a promise rather than a procedure.
 */
export function rewrapDek(wrapped: Sealed, oldKek: Buffer, newKek: Buffer): Sealed {
  return seal(unwrapDek(wrapped, oldKek), newKek);
}

const SECRET_KEY_PATTERN = /token|password|secret|authorization|cookie|credential|refresh|access_key/i;

/**
 * The log serialiser. "Plaintext secrets exist only inside the connector
 * process, only for the length of one call, and are never placed in a variable
 * that a logger can reach" — this is the second half of that sentence.
 */
export function redact<T>(value: T): T {
  const walk = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(walk);
    if (Buffer.isBuffer(node)) return '[buffer]';
    if (node && typeof node === 'object') {
      const out: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
        out[k] = SECRET_KEY_PATTERN.test(k) ? '[redacted]' : walk(v);
      }
      return out;
    }
    return node;
  };
  return walk(value) as T;
}
