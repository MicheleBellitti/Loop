"""The HTTP surface.

Built against one success condition: the existing React PWA, unmodified, points
at this and works. That makes the client the specification, and it settles two
questions before any route is written.

**Same origin.** The client fetches with `credentials: 'same-origin'` and the
content-security policy says `connect-src 'self'`. There is no CORS anywhere,
because there is nowhere for it to go: this app serves the built client itself.
Putting the API on another host would mean editing the client, which fails the
condition it exists to meet.

**Plain dicts, not response models.** A response model reorders keys, drops the
ones that are None and coerces types — and the difference between an absent key
and a null one is load-bearing here. Every handler returns a dict and it is
serialised as written.
"""

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from loop.db import Database
from loop.services.push import VapidConfig

from . import auth
from .errors import INTERNAL_MESSAGE, ApiError, code_for, envelope
from .routes import (
    account,
    applications,
    export,
    health,
    mailboxes,
    metrics,
    passkeys,
    push,
    review,
    session,
    stats,
    stream,
    suggestions,
    today,
)

# Everything the client is allowed to load, and nowhere it may talk to but here.
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

_SECURITY_HEADERS = {
    "content-security-policy": _CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "geolocation=(), microphone=(), camera=()",
}

# A path with a short extension is a file that does not exist, not a route the
# single-page app should be given a chance to render.
_LOOKS_LIKE_A_FILE_SUFFIX = (2, 5)


@dataclass(frozen=True, slots=True)
class WebAuthn:
    """The relying party: who the authenticator thinks it is signing in to.

    `rp_id` must be the registrable domain the client is served from — a
    credential enrolled against one is refused by another, which is the whole
    of the phishing resistance.
    """

    rp_id: str = "localhost"
    rp_name: str = "Loop"


@dataclass(frozen=True, slots=True)
class GoogleApp:
    """The OAuth client. Unset means the connect button is not offered."""

    client_id: str | None = None
    client_secret: str | None = None
    pubsub_topic: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


@dataclass(frozen=True, slots=True)
class Settings:
    dsn: str
    session_secret: str
    public_origin: str = "http://localhost:3000"
    client_dir: Path | None = None
    # The public half reaches the browser through `/api/push/key`; the private
    # half never leaves the notifier.
    vapid: VapidConfig = field(default_factory=VapidConfig)
    # Unset means the model rung is off, which is the default posture and what
    # `/health/deep` reports as `disabled` rather than as a fault.
    model_base_url: str | None = None
    google: GoogleApp = field(default_factory=GoogleApp)
    webauthn: WebAuthn = field(default_factory=WebAuthn)

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https:")

    @classmethod
    def from_env(cls) -> "Settings":
        client = os.environ.get("CLIENT_DIR")
        return cls(
            dsn=os.environ["DATABASE_URL"],
            session_secret=os.environ.get("SESSION_SECRET", "dev-secret"),
            public_origin=os.environ.get("PUBLIC_ORIGIN", "http://localhost:3000"),
            client_dir=Path(client) if client else _default_client_dir(),
            vapid=VapidConfig(
                public_key=_trimmed("VAPID_PUBLIC"),
                private_key=_trimmed("VAPID_PRIVATE"),
                subject=os.environ.get("VAPID_SUBJECT", "mailto:loop@localhost"),
            ),
            model_base_url=_trimmed("MODEL_BASE_URL"),
            google=GoogleApp(
                client_id=_trimmed("GOOGLE_CLIENT_ID"),
                client_secret=_trimmed("GOOGLE_CLIENT_SECRET"),
                pubsub_topic=_trimmed("GOOGLE_PUBSUB_TOPIC"),
            ),
            webauthn=WebAuthn(
                rp_id=os.environ.get("RP_ID", "localhost"),
                rp_name=os.environ.get("RP_NAME", "Loop"),
            ),
        )


def _trimmed(name: str) -> str | None:
    """An empty environment variable is an unset one.

    Compose writes `VAPID_PUBLIC=` for a key nobody has generated yet, and an
    empty string that reads as "configured" is how a deployment comes to think
    it can send notifications.
    """
    return (os.environ.get(name) or "").strip() or None


def _default_client_dir() -> Path | None:
    built = Path(__file__).resolve().parents[3] / "client" / "dist"
    return built if built.is_dir() else None


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # `DB_ROLE`, like every other container: compose runs this one as
        # `loop_gateway`, and its grants are what stop a request from writing a
        # row only the pipeline may write.
        #
        # The auth tables are reached another way. Their policies are FORCEd and
        # read a tenant a signing-in request has not established yet, so those
        # reads go through `Database.untenanted`, which takes no role and
        # therefore meets no policy — see the note there before changing either.
        async with Database(settings.dsn) as db:
            app.state.db = db
            app.state.sessions = auth.Sessions(db, settings.session_secret)
            app.state.settings = settings
            # One `listen` per process, opened here so a stream that arrives a
            # millisecond after boot has something to attach to.
            broadcaster = stream.Broadcaster(settings.dsn)
            await broadcaster.start()
            app.state.broadcaster = broadcaster
            try:
                yield
            finally:
                await broadcaster.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    _install_error_handling(app)
    # The gate is registered before the headers so the headers end up *outside*
    # it: Starlette runs the most recently added middleware first, and a 401
    # from the gate still has to carry the same policy as every other response.
    _install_gate(app)
    _install_security_headers(app)

    app.include_router(session.router)
    app.include_router(passkeys.router)
    app.include_router(today.router)
    app.include_router(applications.router)
    app.include_router(review.router)
    app.include_router(stats.router)
    app.include_router(suggestions.router)
    app.include_router(push.router)
    app.include_router(mailboxes.router)
    app.include_router(export.router)
    app.include_router(account.router)
    app.include_router(stream.router)
    app.include_router(health.router)
    app.include_router(metrics.router)

    _install_client(app, settings)
    return app


def _install_error_handling(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _coded(_request: Request, error: ApiError) -> JSONResponse:
        return JSONResponse(error.body(), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def _unparseable(_request: Request, error: RequestValidationError) -> JSONResponse:
        """A query or path parameter FastAPI would not accept.

        Its own handler answers `{"detail": [...]}`, which is the one response
        shape on this API that is not the envelope — and the client reads
        `error.code` off every failure. `?limit=nonsense` on the applications
        list is enough to reach it, so this is a live route rather than a
        theoretical one.
        """
        first = (error.errors() or [{}])[0]
        # `("query", "limit")` — the location, minus the part naming where it
        # came from, which the client has no use for.
        location = [str(part) for part in first.get("loc", ())][1:]
        return JSONResponse(
            envelope(
                code_for(422),
                str(first.get("msg") or "that is not a valid request"),
                ".".join(location) or None,
            ),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def _uncoded(request: Request, error: Exception) -> JSONResponse:
        # A 500 loses its message on the way out. Whatever went wrong is worth
        # a log line and is never worth a browser seeing the SQL.
        status = getattr(error, "status_code", 500)
        code = code_for(status)
        message = INTERNAL_MESSAGE if status >= 500 else str(error)
        response = JSONResponse(envelope(code, message), status_code=status)
        # Applied here as well as in the middleware, because this handler does
        # not run behind it. Starlette hangs a bare `Exception` handler off
        # `ServerErrorMiddleware`, which is the outermost layer of the stack —
        # so a 500 was the one response that left without a content-security
        # policy or `nosniff`, which is exactly the response an attacker would
        # rather have.
        return _secured(response, request)


def _secured[T: Response](response: T, request: Request) -> T:
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.public_origin.startswith("https:"):
        response.headers.setdefault(
            "strict-transport-security", "max-age=31536000; includeSubDomains"
        )
    return response


def _install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        return _secured(await call_next(request), request)


def _install_gate(app: FastAPI) -> None:
    @app.middleware("http")
    async def _gate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if not auth.guarded(path) or auth.is_public(path):
            request.state.session = None
            return await call_next(request)

        sessions: auth.Sessions = request.app.state.sessions
        found = await sessions.load(request.cookies.get(auth.COOKIE_NAME))
        if found is None:
            error = auth.unauthenticated()
            return JSONResponse(error.body(), status_code=error.status)

        if auth.gate_needs_csrf(request.method):
            try:
                sessions.check_csrf(found, request.headers.get("x-csrf-token"))
            except ApiError as error:
                return JSONResponse(error.body(), status_code=error.status)

        request.state.session = found
        return await call_next(request)


def _install_client(app: FastAPI, settings: Settings) -> None:
    """Serve the built client, and give the router its paths back.

    Registered last so it cannot shadow the API, and split three ways on a miss
    because the single-page app needs its own routes to reach `index.html`
    while a missing asset must still be a 404.
    """
    directory = settings.client_dir
    index = directory / "index.html" if directory else None

    if directory and (directory / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=directory / "assets"), name="assets")

    @app.get("/health")
    async def _health() -> dict[str, object]:
        return {"ok": True}

    @app.get("/{filename}")
    async def _root_file(filename: str) -> Response:
        """The handful of files that have to sit at the root to work at all.

        `sw.js` is the reason this exists. A service worker's scope is the
        directory it is served from, so one served from `/assets` could only
        control `/assets` — it has to be at the root or it controls nothing.
        `manifest.webmanifest` and the icons are the same story for a different
        reason: the manifest is what makes the app installable, and a browser
        looks for it where the page said it was.

        Registered after every route that means something, so it can only ever
        answer for a path nothing else claimed, and it answers only for a file
        that is really there — `/pipeline` falls through to the single-page
        handler below, as it must.
        """
        candidate = directory / filename if directory else None
        if (
            candidate is None
            or filename != Path(filename).name
            or not candidate.is_file()
        ):
            # A plain 404 rather than a coded one, deliberately: this has to
            # fall through to the handler below, which is what decides between
            # a missing file and a route the single-page app owns.
            raise HTTPException(status_code=404)
        return FileResponse(candidate)

    @app.exception_handler(404)
    async def _missing(request: Request, _error: Exception) -> Response:
        path = request.url.path
        if path.startswith("/api"):
            return JSONResponse(envelope("not_found", "no such endpoint"), status_code=404)
        if _looks_like_a_file(path) or "text/html" not in request.headers.get("accept", ""):
            return JSONResponse(envelope("not_found", "no such file"), status_code=404)
        if index and index.is_file():
            return FileResponse(index)
        return JSONResponse(envelope("not_found", "no such file"), status_code=404)


def _looks_like_a_file(path: str) -> bool:
    suffix = Path(path).suffix.lstrip(".")
    low, high = _LOOKS_LIKE_A_FILE_SUFFIX
    return low <= len(suffix) <= high


def app_from_env() -> FastAPI:
    return create_app(Settings.from_env())
