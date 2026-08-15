"""The integration-test database.

    uv run python scripts/test_db.py up
    uv run python scripts/test_db.py down

`up` starts the same image compose uses — the extensions and the `pg_cron`
preload have to be identical, or the tests prove something about a database
nobody deploys. Port 55432, deliberately not 5432, so it cannot collide with one
you already run, and the password is the throwaway `loop`.

Ported from `scripts/test-db.mjs` because CI's Python job called it, which meant
the Python gates depended on Node being installed to run at all.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

NAME = "loop-pg-test"
IMAGE = "loop-postgres:16"
PORT = "55432"
READY_TIMEOUT_SECONDS = 60


def _docker(*args: str, check: bool = True, quiet: bool = False) -> int:
    output = subprocess.DEVNULL if quiet else None
    return subprocess.run(
        ["docker", *args], check=check, stdout=output, stderr=output
    ).returncode


def _image_exists() -> bool:
    return _docker("image", "inspect", IMAGE, check=False, quiet=True) == 0


def up(port: str) -> None:
    if not _image_exists():
        print(f"building {IMAGE}…")
        _docker("build", "-t", IMAGE, str(_repo_root() / "infra" / "postgres"))

    _docker("rm", "-f", NAME, check=False, quiet=True)
    _docker(
        "run",
        "-d",
        "--name",
        NAME,
        "-e",
        "POSTGRES_USER=loop",
        "-e",
        "POSTGRES_PASSWORD=loop",
        "-e",
        "POSTGRES_DB=loop",
        "-p",
        f"{port}:5432",
        IMAGE,
    )

    sys.stdout.write("waiting for postgres")
    sys.stdout.flush()
    for _ in range(READY_TIMEOUT_SECONDS):
        ready = _docker(
            "exec", NAME, "pg_isready", "-U", "loop", "-d", "loop", check=False, quiet=True
        )
        if ready == 0:
            print(f"\nready · DATABASE_URL=postgres://loop:loop@localhost:{port}/loop")
            return
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1)
    raise SystemExit("\npostgres did not become ready")


def down() -> None:
    _docker("rm", "-f", NAME, check=False)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "down"), nargs="?", default="up")
    parser.add_argument("--port", default=PORT)
    args = parser.parse_args()

    if args.action == "up":
        up(args.port)
    else:
        down()


if __name__ == "__main__":
    main()
