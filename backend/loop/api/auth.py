"""Sessions, the derived CSRF token, and the gate in front of every route.

Two things here are easy to get wrong in ways that lock everyone out.

**The auth tables are read on an owner connection.** `sessions`, `credentials`,
`auth_secrets` and `users` all have row-level security enabled and forced, and
their policies read `loop.user_id` — which is exactly what a request has not
established yet when it is trying to find out who is asking. The reference runs
these queries on the raw pool and isolates by a hand-written `where user_id`.
Routing them through the tenant wrapper, or connecting as `loop_gateway`,
returns zero rows for everything and nobody can sign in.

**The CSRF token is derived, never stored.** An HMAC over the session id, so it
is stable for the life of the session and recomputable on any request without a
lookup. An earlier version returned the stored hash, which made the token
unrecoverable after a reload.
"""

import asyncio
import base64
import hashlib
import hmac
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loop.db import Database

from .errors import ApiError, bad_csrf
from .errors import unauthenticated as _unauthenticated

COOKIE_NAME = "loop_session"
SESSION_DAYS = 30
SESSION_MAX_AGE = SESSION_DAYS * 24 * 60 * 60

# Exact string match, no normalisation, fails closed. A path not in here needs a
# session — including one that does not exist, which is why an unauthenticated
# request to a nonexistent `/api` route is a 401 and not a 404.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/deep",
        "/metrics",
        "/api/auth/state",
        "/api/auth/login/options",
        "/api/auth/login/verify",
        "/api/auth/recover",
        "/api/auth/session-check",
        "/api/gmail/push",
    }
)

_SAFE_METHODS = frozenset({"GET", "HEAD"})

# scrypt, with the parameters the seed script wrote. Verification re-reads them
# from the stored string so an older hash keeps working.
_SCRYPT_MAXMEM = 268_435_456
_SCRYPT_DKLEN = 64


@dataclass(frozen=True, slots=True)
class Session:
    id: str
    user_id: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def new_token() -> str:
    """43 unpadded base64url characters, from 32 random bytes."""
    return _b64url(secrets.token_bytes(32))


def token_hash(token: str) -> bytes:
    """The digest is over the 43 characters, not the bytes behind them."""
    return hashlib.sha256(token.encode()).digest()


def csrf_for(session_id: str, secret: str) -> str:
    digest = hmac.new(secret.encode(), f"csrf:{session_id}".encode(), hashlib.sha256).digest()
    return _b64url(digest)


def csrf_matches(presented: str | None, expected: str) -> bool:
    if not presented or len(presented) != len(expected):
        return False
    return hmac.compare_digest(presented, expected)


class Sessions:
    """Everything that reads an auth table, on the owner connection."""

    def __init__(self, db: Database, secret: str) -> None:
        self._db = db
        self._secret = secret

    async def create(self, user_id: str) -> tuple[str, Session]:
        token = new_token()
        expires_at = datetime.now(UTC) + timedelta(days=SESSION_DAYS)
        async with self._db.untenanted() as connection:
            session_id = await connection.fetchval(
                """
                insert into sessions (user_id, token_hash, expires_at)
                values ($1,$2,$3) returning id
                """,
                user_id,
                token_hash(token),
                expires_at,
            )
        return token, Session(str(session_id), user_id)

    async def load(self, token: str | None) -> Session | None:
        """Find the session and stamp it as seen.

        A write, so this cannot be served from a read-only transaction. Expiry
        is enforced in the predicate rather than in Python: an expired row
        simply does not come back.
        """
        if not token:
            return None
        async with self._db.untenanted() as connection:
            row = await connection.fetchrow(
                """
                update sessions set last_seen_at = now()
                 where token_hash = $1 and expires_at > now()
                 returning id, user_id
                """,
                token_hash(token),
            )
        return Session(str(row["id"]), str(row["user_id"])) if row else None

    async def destroy(self, token: str) -> None:
        async with self._db.untenanted() as connection:
            await connection.execute(
                "delete from sessions where token_hash = $1", token_hash(token)
            )

    def csrf(self, session: Session) -> str:
        return csrf_for(session.id, self._secret)

    def check_csrf(self, session: Session, presented: str | None) -> None:
        if not csrf_matches(presented, self.csrf(session)):
            raise bad_csrf()


async def sole_user(db: Database) -> tuple[str, str] | None:
    """The one account. Single tenancy is a `limit 1`, not a constraint."""
    async with db.untenanted() as connection:
        row = await connection.fetchrow(
            "select id, email from users order by created_at limit 1"
        )
    return (str(row["id"]), row["email"]) if row else None


async def has_passkey(db: Database, user_id: str) -> bool:
    async with db.untenanted() as connection:
        return bool(
            await connection.fetchval(
                "select count(*) from credentials where user_id = $1", user_id
            )
        )


async def check_recovery_password(db: Database, user_id: str, password: str) -> bool:
    """The recovery code, which is currently the only way into the product."""
    async with db.untenanted() as connection:
        stored = await connection.fetchval(
            "select recovery_hash from auth_secrets where user_id = $1", user_id
        )
    if not stored:
        return False
    # Off the event loop. At the seeded parameters (N=2^16, r=8, p=2) this is
    # most of a second and 64MB, and `/api/auth/recover` is public — a handful
    # of concurrent guesses would otherwise serialise into a stall that takes
    # every other request down with them, `/health` included. The reference got
    # this for free by promisifying node's scrypt onto the threadpool.
    return await asyncio.to_thread(verify_scrypt, password, stored)


# What a fresh hash is written with. Verification reads the parameters back out
# of the stored string instead, so raising these does not invalidate anything
# already on disk.
_SCRYPT_N = 2**16
_SCRYPT_R = 8
_SCRYPT_P = 2


def hash_scrypt(password: str) -> str:
    """`scrypt$N$r$p$<salt>$<hash>`, byte-compatible with the reference.

    Sixteen bytes of salt, sixty-four of output, and NFKC first so a password
    typed with a composed accent verifies against one stored with a combining
    one. Roughly a second and 64MB per call at these parameters, which is the
    point: `/api/auth/recover` is public.
    """
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        unicodedata.normalize("NFKC", password).encode(),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(derived).decode(),
        )
    )


def verify_scrypt(password: str, stored: str) -> bool:
    """`scrypt$N$r$p$<salt>$<hash>`, both base64 with padding.

    The parameters come out of the stored string rather than from a constant, so
    a hash written under older settings still verifies. CPython needs `maxmem`
    stated explicitly or it refuses at these sizes.
    """
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        derived = hashlib.scrypt(
            unicodedata.normalize("NFKC", password).encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=_SCRYPT_DKLEN,
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, base64.b64decode(expected))


async def mark_recovery_used(db: Database, user_id: str) -> None:
    async with db.untenanted() as connection:
        await connection.execute(
            "update auth_secrets set recovery_used_at = now() where user_id = $1", user_id
        )


def require(session: Session | None) -> Session:
    if session is None:
        raise _unauthenticated()
    return session


def gate_needs_csrf(method: str) -> bool:
    return method.upper() not in _SAFE_METHODS


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS


def guarded(path: str) -> bool:
    """Whether the gate applies at all. Static assets are none of its business."""
    return path.startswith("/api") or path == "/metrics"


def session_cookie(token: str, *, secure: bool) -> str:
    """Serialised by hand so the attribute order matches the reference's.

    `Secure` sits between `HttpOnly` and `SameSite`, and `secure` is decided
    once from the configured origin rather than per request — a client behind a
    proxy that terminates TLS would otherwise get a cookie it cannot send back.
    """
    parts = [f"{COOKIE_NAME}={token}", f"Max-Age={SESSION_MAX_AGE}", "Path=/", "HttpOnly"]
    if secure:
        parts.append("Secure")
    parts.append("SameSite=Lax")
    return "; ".join(parts)


def cleared_cookie() -> str:
    return (
        f"{COOKIE_NAME}=; Max-Age=0; Path=/; "
        "Expires=Thu, 01 Jan 1970 00:00:00 GMT; SameSite=Lax"
    )


def password_error() -> ApiError:
    return ApiError(401, "bad_password", "wrong password")


def unauthenticated() -> ApiError:
    """Re-exported so the gate and the routes raise the same thing."""
    return _unauthenticated()
