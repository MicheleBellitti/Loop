"""End to end, against the stub mailbox.

    uv run --extra api --extra db --extra connector --extra ladder \
        python scripts/e2e.py

Starts the five pipeline services for real, connects a mailbox pointed at the
fixture-replay server, runs a backfill, and asserts that applications appear with
events, provenance and confidence attached.

Every component in the path is the production one: only Google is stubbed. That
is what makes this worth running — it is the one test that exercises the queue,
the roles, the row-level security and all five services together, and it is the
answer to "does the pipeline actually work" that no unit test can give.

The services run as subprocesses rather than as tasks in this one, deliberately.
Each has its own database role, and a role is a property of a connection: five
services sharing one process would share one pool, and the grants that make the
single-writer rule real would go untested.
"""

import argparse
import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

import asyncpg

from loop.google.crypto import generate_dek, seal, wrap_dek

DEFAULT_DSN = "postgres://loop:loop@localhost:55432/loop"
STUB_PORT = os.environ.get("STUB_PORT", "8787")
SETTLE_ROUNDS = 60


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class Fleet:
    """The processes, and the promise that they all stop."""

    def __init__(self, env: dict[str, str], *, verbose: bool) -> None:
        self._env = env
        self._verbose = verbose
        self._children: list[subprocess.Popen[bytes]] = []

    def start(self, name: str, *args: str, role: str | None = None) -> None:
        env = dict(self._env)
        if role:
            env["DB_ROLE"] = role
        output = None if self._verbose else subprocess.DEVNULL
        self._children.append(
            subprocess.Popen(
                [sys.executable, *args],
                env=env,
                stdout=output,
                stderr=None if self._verbose else subprocess.DEVNULL,
                cwd=str(repo_root() / "python"),
            )
        )

    def stop(self) -> None:
        for child in self._children:
            with suppress(ProcessLookupError):
                child.send_signal(signal.SIGTERM)
        for child in self._children:
            with suppress(subprocess.TimeoutExpired):
                child.wait(timeout=10)
        for child in self._children:
            if child.poll() is None:
                child.kill()


def fail(message: str, detail: str = "") -> None:
    print(f"\n  ✗ {message}", file=sys.stderr)
    if detail:
        print(f"    {detail}", file=sys.stderr)
    raise SystemExit(1)


async def run(dsn: str, *, verbose: bool) -> None:
    print("\n  end to end · stub mailbox → applications\n")

    kek = os.environ.get("LOOP_KEK") or base64.b64encode(bytes([3]) * 32).decode()
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "LOOP_KEK": kek,
        "SESSION_SECRET": "e2e",
        "GOOGLE_CLIENT_ID": "stub",
        "GOOGLE_CLIENT_SECRET": "stub",
        "GOOGLE_API_BASE": f"http://localhost:{STUB_PORT}",
        "GOOGLE_OAUTH_BASE": f"http://localhost:{STUB_PORT}",
        # The model is off, which is the default posture: what only rung 3
        # could place becomes a review item, and that is failure state F4.
        "MODEL_BASE_URL": "",
        "LOG_LEVEL": os.environ.get("LOG_LEVEL", "WARNING"),
        "QUIET_HOURS": "21:00-08:00",
    }

    connection = await asyncpg.connect(dsn)
    fleet = Fleet(env, verbose=verbose)
    user_id: str | None = None
    try:
        # ── a clean tenant ───────────────────────────────────────────────────
        user_id = str(
            await connection.fetchval(
                "insert into users (email, tz) values ($1, 'Europe/Rome') returning id",
                f"e2e-{uuid.uuid4().hex[:12]}@example.com",
            )
        )
        await connection.execute("select seed_stage_defs($1)", user_id)
        print(f"  · tenant {user_id[:8]}")

        # ── a mailbox pointing at the stub ───────────────────────────────────
        dek = generate_dek()
        wrapped = wrap_dek(dek, base64.b64decode(kek))
        sealed = seal(json.dumps({"refresh_token": "stub-refresh-token"}).encode(), dek)
        mailbox_id = str(
            await connection.fetchval(
                """
                insert into mailbox_accounts
                  (user_id, provider, address, secret_ciphertext, secret_nonce,
                   dek_wrapped, dek_nonce, scopes, status)
                values ($1,'gmail',$2,$3,$4,$5,$6,'{gmail.readonly}','ok')
                returning id
                """,
                user_id,
                "you@example.com",
                sealed.ciphertext,
                sealed.nonce,
                wrapped.ciphertext,
                wrapped.nonce,
            )
        )
        print(f"  · mailbox {mailbox_id[:8]} → stub")

        # ── the stub, then the services ─────────────────────────────────────
        fleet.start("stub", "scripts/stub_google.py", "--port", STUB_PORT)
        await asyncio.sleep(1.0)

        for name, role in (
            ("classifier", "loop_classifier"),
            ("extractor", "loop_extractor"),
            ("resolver", "loop_resolver"),
            ("pipeline", "loop_pipeline"),
            ("connector", "loop_connector"),
        ):
            fleet.start(name, "-m", "loop", name, role=role)
        print("  · five services up")
        await asyncio.sleep(3.5)

        # ── the first scan ──────────────────────────────────────────────────
        await connection.execute(
            "select pg_notify($1, $2)",
            "loop_backfill",
            json.dumps({"mailbox_id": mailbox_id, "months": 12}),
        )
        print("  · backfill requested")

        applications, events = await _settle(connection, user_id)
        await _assert_the_pipeline_ran(connection, user_id, applications, events)
        await _report(connection, user_id)
    finally:
        fleet.stop()
        if user_id:
            with suppress(Exception):
                await connection.execute("select erase_user($1)", user_id)
        await connection.close()


async def _settle(connection: asyncpg.Connection, user_id: str) -> tuple[int, int]:
    """Wait until two consecutive rounds see the same counts."""
    applications = events = 0
    for round_number in range(SETTLE_ROUNDS):
        await asyncio.sleep(1.0)
        now_applications = await connection.fetchval(
            "select count(*) from applications where user_id = $1", user_id
        )
        now_events = await connection.fetchval(
            "select count(*) from application_events where user_id = $1", user_id
        )
        settled = (
            now_applications == applications and now_events == events and applications > 0
        )
        applications, events = now_applications, now_events
        sys.stdout.write(f"\r  · {applications} applications, {events} events   ")
        sys.stdout.flush()
        if settled and round_number > 6:
            break
    print("")
    return applications, events


async def _assert_the_pipeline_ran(
    connection: asyncpg.Connection, user_id: str, applications: int, events: int
) -> None:
    outcomes = {
        row["outcome"] or "pending": row["n"]
        for row in await connection.fetch(
            "select outcome, count(*) as n from seen_messages"
            " where user_id = $1 group by outcome order by outcome",
            user_id,
        )
    }
    print(f"  · messages {json.dumps(outcomes)}")

    if applications == 0:
        fail("no application was created from the stub mailbox")
    if events == 0:
        fail("no event reached the log")
    if not outcomes.get("dropped"):
        fail("the classifier dropped nothing — the negatives should not have survived")
    if not outcomes.get("placed"):
        fail("nothing was placed")

    # Provenance and confidence on every automated event.
    unprovenanced = await connection.fetchval(
        """
        select count(*) from application_events
         where user_id = $1 and rung is not null and rung < 4
           and (evidence_ref is null or confidence is null)
        """,
        user_id,
    )
    if unprovenanced:
        fail(f"{unprovenanced} automated events lack evidence or confidence")

    # Exactly one first touch per application.
    doubled = await connection.fetch(
        "select application_id from sources where user_id = $1 and is_first_touch"
        " group by application_id having count(*) > 1",
        user_id,
    )
    if doubled:
        fail("an application has more than one first touch")


async def _report(connection: asyncpg.Connection, user_id: str) -> None:
    rows = await connection.fetch(
        """
        select c.canonical_name as company, a.role_title, a.current_stage,
               a.current_phase, a.confidence,
               (select count(*) from application_events e
                 where e.application_id = a.id) as events
          from applications a join companies c on c.id = a.company_id
         where a.user_id = $1 order by c.canonical_name limit 8
        """,
        user_id,
    )
    print("\n  applications")
    for row in rows:
        print(
            f"  · {row['company']:<22} {row['current_stage']:<14}"
            f" {row['current_phase']:<13} conf {row['confidence']}  {row['events']} events"
        )

    review = await connection.fetchval(
        "select count(*) from review_items where user_id = $1 and resolved_at is null",
        user_id,
    )
    print(f"\n  review queue: {review} item(s) — the messages only rung 3 could place")
    print(
        "\n  ✓ a message went from the mailbox to the pipeline"
        " without anyone typing anything\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn", default=os.environ.get("TEST_DATABASE_URL") or DEFAULT_DSN
    )
    parser.add_argument("--verbose", action="store_true", help="show every service's log")
    args = parser.parse_args()

    started = time.monotonic()
    asyncio.run(run(args.dsn, verbose=args.verbose))
    print(f"  {time.monotonic() - started:.1f}s\n")


if __name__ == "__main__":
    main()
