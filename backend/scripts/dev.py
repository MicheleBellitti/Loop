"""Every service in one terminal, each with its own database role.

    uv run --extra api --extra db --extra connector --extra push --extra ladder \
        python scripts/dev.py

Under compose every service is handed its environment by the orchestrator. Here
nothing does, so this loads `.env` once and the children inherit it — otherwise
every service boots and immediately dies on a missing `DATABASE_URL`, which is a
confusing way to learn that a file was not read.

The roles are the point. Each service connects as its own, so row-level security
and the per-service grants apply exactly as they do in production: run the
resolver and try to append an event and Postgres says no, here as there. Running
them all in one process would share one pool and one role, and every grant in
migration 003 would go back to being decorative.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
from contextlib import suppress
from pathlib import Path

# The API first, because it is the one you open. `python -m loop <name>` maps
# each of these to its own role in `loop/__main__.py`, so nothing is repeated.
SERVICES = (
    "api",
    "connector",
    "classifier",
    "extractor",
    "resolver",
    "pipeline",
    "nudge",
    "notifier",
)

WIDTH = max(len(name) for name in SERVICES)


def repo_root() -> Path:
    """The repository, where `.env` lives — one above `backend/`."""
    return Path(__file__).resolve().parents[2]


def load_env(path: Path) -> dict[str, str]:
    """`.env`, read the way compose reads it: `KEY=value`, no interpolation.

    Including the precedence. Compose resolves the shell environment *ahead* of
    the file, and seeding from `os.environ` and then writing over it does the
    opposite — so `DATABASE_URL=… python scripts/dev.py`, which is how the
    README says to point the fleet at the throwaway test database, silently ran
    against whatever is committed in `.env`.
    """
    if not path.is_file():
        raise SystemExit(f"no {path.name} found — copy .env.example and fill it in")
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        env[key.strip()] = _unquoted(value.strip())
    return env | os.environ


def _unquoted(value: str) -> str:
    """One matched pair of quotes, not every quote at either end."""
    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            return value[1:-1]
    return value


def pump(name: str, stream: object, out: object) -> None:
    prefix = f"{name:<{WIDTH}} │ "
    for line in stream:  # type: ignore[attr-defined]
        out.write(prefix + line)  # type: ignore[attr-defined]
        out.flush()  # type: ignore[attr-defined]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=repo_root() / ".env")
    parser.add_argument(
        "--only", nargs="*", choices=SERVICES, help="start a subset, for debugging one"
    )
    args = parser.parse_args()

    env = load_env(args.env_file)
    wanted = tuple(args.only) if args.only else SERVICES

    children: list[subprocess.Popen[str]] = []
    for name in wanted:
        child = subprocess.Popen(
            [sys.executable, "-m", "loop", name],
            env=env,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        children.append(child)
        threading.Thread(
            target=pump, args=(name, child.stdout, sys.stdout), daemon=True
        ).start()

    def stop(*_: object) -> None:
        for child in children:
            with suppress(ProcessLookupError):
                child.send_signal(signal.SIGTERM)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        for child, name in zip(children, wanted, strict=True):
            code = child.wait()
            if code != 0:
                print(f"{name:<{WIDTH}} │ exited with {code}", file=sys.stderr)
    finally:
        stop()


if __name__ == "__main__":
    main()
