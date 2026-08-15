"""Envelope encryption, which had no test in Python at all.

A refresh token to a mailbox is the whole of the trust the user extends. The
claim this module makes — a stolen dump yields nothing readable — is the only
claim worth making about a box that reads your mail, and until now nothing
checked it on this side of the port.

Four assertions, from `packages/db/src/db.itest.ts`: the round trip, the
rotation, that the old key stops working after one, and that the wrong key
raises rather than returning plausible bytes.
"""

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from loop.google.crypto import (
    KEY_BYTES,
    generate_dek,
    load_kek,
    open_sealed,
    rewrap_dek,
    seal,
    unwrap_dek,
    wrap_dek,
)

SECRET = b"1//0gRefreshTokenThatUnlocksAMailbox"


def a_key() -> bytes:
    return os.urandom(KEY_BYTES)


class TestSealingASecret:
    def test_seals_and_opens(self) -> None:
        kek = a_key()
        dek = generate_dek()
        sealed = seal(SECRET, dek)
        assert sealed.ciphertext != SECRET
        assert open_sealed(sealed, dek) == SECRET

        wrapped = wrap_dek(dek, kek)
        assert unwrap_dek(wrapped, kek) == dek

    def test_the_nonce_is_never_reused(self) -> None:
        # Twelve bytes is small enough to matter at volume and does not here,
        # but "does not" has to be because they are random, not because the
        # count is low.
        dek = generate_dek()
        nonces = {seal(SECRET, dek).nonce for _ in range(100)}
        assert len(nonces) == 100

    def test_a_stolen_ciphertext_is_unreadable_with_the_wrong_key(self) -> None:
        sealed = seal(SECRET, a_key())
        with pytest.raises(InvalidTag):
            open_sealed(sealed, a_key())

    def test_tampering_with_the_ciphertext_is_caught(self) -> None:
        # The AEAD tag, doing the job it is there for. A mis-set LOOP_KEK is a
        # loud failure at the first sync rather than a mailbox that mysteriously
        # stops authenticating.
        key = a_key()
        sealed = seal(SECRET, key)
        flipped = bytearray(sealed.ciphertext)
        flipped[0] ^= 0x01
        with pytest.raises(InvalidTag):
            open_sealed(type(sealed)(bytes(flipped), sealed.nonce), key)


class TestRotatingTheKey:
    def test_rewraps_a_data_key_and_leaves_the_secret_alone(self) -> None:
        old, new = a_key(), a_key()
        dek = generate_dek()
        sealed_secret = seal(SECRET, dek)
        wrapped = wrap_dek(dek, old)

        rewrapped = rewrap_dek(wrapped, old, new)

        # The point of the envelope: the ciphertext never moved.
        assert unwrap_dek(rewrapped, new) == dek
        assert open_sealed(sealed_secret, unwrap_dek(rewrapped, new)) == SECRET

    def test_and_the_old_key_stops_working(self) -> None:
        old, new = a_key(), a_key()
        rewrapped = rewrap_dek(wrap_dek(generate_dek(), old), old, new)
        with pytest.raises(InvalidTag):
            unwrap_dek(rewrapped, old)

    def test_rotating_with_the_wrong_old_key_raises_rather_than_corrupting(self) -> None:
        # Better to fail the whole transaction than to write a data key that
        # unwraps to noise, which nothing would notice until the next sync.
        wrapped = wrap_dek(generate_dek(), a_key())
        with pytest.raises(InvalidTag):
            rewrap_dek(wrapped, a_key(), a_key())


class TestLoadingTheKey:
    def test_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = a_key()
        monkeypatch.setenv("LOOP_KEK", base64.b64encode(key).decode())
        assert load_kek() == key

    def test_refuses_a_key_of_the_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="32 bytes"):
            load_kek(base64.b64encode(os.urandom(16)).decode())

    def test_says_how_to_make_one_when_it_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LOOP_KEK", raising=False)
        with pytest.raises(ValueError, match="LOOP_KEK is not set"):
            load_kek()
