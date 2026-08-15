"""Rotate the key-encryption key.

    LOOP_KEK_OLD=… LOOP_KEK=… uv run --extra db --extra connector \
        python scripts/rotate_kek.py

Every data key is re-wrapped in one transaction. Nothing else moves: the sealed
refresh tokens stay exactly as they are, which is the whole reason for the
envelope — rotating the outer key is a few rows, not a re-encryption of every
secret.

`docs/runbook.md` promises this works, and until now the promise had no Python
behind it. `tests/test_crypto.py` covers the primitive; this is the loop over
the table.
"""

import asyncio
import os

from loop.db import Database
from loop.google.crypto import Sealed, load_kek, rewrap_dek


def _key(name: str) -> bytes:
    """`load_kek`, which is what every service decodes a key with.

    It takes an explicit value for exactly this caller. A second decoder here
    was a second opinion about what a valid key is — this one used
    `validate=True`, `load_kek` does not — so a `LOOP_KEK` with a stray
    character could be accepted by the connector and rejected by the rotation,
    or, the other way round, re-wrap every data key under a key the connector
    cannot reproduce.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        raise SystemExit(f"{name} is not set. Both keys are base64, 32 bytes.")
    try:
        return load_kek(raw)
    except ValueError as error:
        raise SystemExit(f"{name}: {error}") from error


async def rotate(dsn: str, old_kek: bytes, new_kek: bytes) -> int:
    """One transaction, `for update`, every row or none of them.

    A rotation that half-finished would leave some mailboxes readable under the
    old key and some under the new one, with no record of which — and the
    recovery from that is reconnecting every mailbox.
    """
    async with (
        Database(dsn, role=None) as db,
        db.untenanted() as connection,
        connection.transaction(),
    ):
        rows = await connection.fetch(
            "select id, dek_wrapped, dek_nonce from mailbox_accounts for update"
        )
        for row in rows:
            wrapped = Sealed(ciphertext=row["dek_wrapped"], nonce=row["dek_nonce"])
            rewrapped = rewrap_dek(wrapped, old_kek, new_kek)
            await connection.execute(
                "update mailbox_accounts"
                " set dek_wrapped = $2, dek_nonce = $3 where id = $1",
                row["id"],
                rewrapped.ciphertext,
                rewrapped.nonce,
            )
        return len(rows)


def main() -> None:
    old_kek = _key("LOOP_KEK_OLD")
    new_kek = _key("LOOP_KEK")
    if old_kek == new_kek:
        raise SystemExit("LOOP_KEK_OLD and LOOP_KEK are the same key; nothing to do")

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is not set")

    rotated = asyncio.run(rotate(dsn, old_kek, new_kek))
    print(f"re-wrapped {rotated} data key(s). Update LOOP_KEK in .env and restart.")


if __name__ == "__main__":
    main()
