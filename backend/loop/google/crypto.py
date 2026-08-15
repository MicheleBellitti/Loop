"""Envelope encryption for the one secret this system holds.

A refresh token to a mailbox is the whole of the trust the user extends, so it
is sealed under a per-mailbox data key, and that key is sealed under a key held
outside the database. A stolen dump yields nothing readable — which is the only
claim worth making about a box that reads your mail.

AES-256-GCM, and the reasoning carries over from the reference unchanged: an
audited AEAD, one mode, nothing to choose wrongly, and no native dependency to
rebuild on every interpreter bump. The nonce is twelve bytes, which is small
enough to matter at volume and does not: a data key seals one secret per
mailbox and the key wraps one data key per user, so the count is in single
digits against a bound of 2^32.

The format is byte-compatible with the TypeScript one — the tag appended to the
ciphertext, the nonce beside it — because both implementations read the same
rows for as long as both run.
"""

import base64
import os
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES: Final = 12
KEY_BYTES: Final = 32


@dataclass(frozen=True, slots=True)
class Sealed:
    """Ciphertext with the sixteen-byte tag appended, and its nonce."""

    ciphertext: bytes
    nonce: bytes


def load_kek(value: str | None = None) -> bytes:
    key_material = value if value is not None else os.environ.get("LOOP_KEK")
    if not key_material:
        raise ValueError(
            "LOOP_KEK is not set. Generate one with: "
            'python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        )
    key = base64.b64decode(key_material)
    if len(key) != KEY_BYTES:
        raise ValueError(f"LOOP_KEK must decode to 32 bytes, got {len(key)}")
    return key


def generate_dek() -> bytes:
    return os.urandom(KEY_BYTES)


def seal(plaintext: bytes, key: bytes) -> Sealed:
    nonce = os.urandom(NONCE_BYTES)
    return Sealed(AESGCM(key).encrypt(nonce, plaintext, None), nonce)


def open_sealed(sealed: Sealed, key: bytes) -> bytes:
    """Raises rather than returning plausible bytes on the wrong key.

    That is the AEAD tag doing its job, and it is why a mis-set `LOOP_KEK` is a
    loud failure at the first sync rather than a mailbox that mysteriously
    stops authenticating.
    """
    plaintext: bytes = AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, None)
    return plaintext


def wrap_dek(dek: bytes, kek: bytes | None = None) -> Sealed:
    return seal(dek, kek or load_kek())


def unwrap_dek(wrapped: Sealed, kek: bytes | None = None) -> bytes:
    return open_sealed(wrapped, kek or load_kek())


def rewrap_dek(wrapped: Sealed, old_kek: bytes, new_kek: bytes) -> Sealed:
    """KEK rotation, one data key at a time.

    `scripts/rotate_kek.py` drives it over every row in one transaction, and a
    test covers it — because the runbook promises rotation works, and an
    untested rotation is a promise rather than a procedure.

    Note what this does *not* touch: the sealed refresh token itself. Rotating
    the key-encryption key re-wraps the data keys and leaves every ciphertext
    where it is, which is the whole reason for the envelope.
    """
    return seal(unwrap_dek(wrapped, old_kek), new_kek)
