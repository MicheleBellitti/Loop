"""One entrypoint, six processes.

    python -m loop connector | classifier | extractor | resolver | pipeline
                   | notifier | nudge | api

Each name starts exactly one service, because each has a different role, a
different rate budget and a different reason to be restarted. The database
enforces the split — only `loop_pipeline` can insert an event — so running two
of them in one process would not be a shortcut, it would be a different system.

`DB_ROLE` is what picks the role, and it is set per container. Leaving it unset
connects as the owner, which is a superuser, and every policy and grant becomes
decorative — which is exactly the state the TypeScript version was in.
"""

import asyncio
import os
import sys

from loop.db import Database, Queue
from loop.runtime import configure_logging
from loop.services import (
    ClassifierService,
    ConsumerOptions,
    ExtractorService,
    NotifierService,
    NudgeService,
    PipelineService,
    ResolverService,
)
from loop.services.connector import ConnectorService
from loop.services.push import VapidConfig
from loop.services.runtime import ConnectorRuntime, Service, until_signalled

_ROLES = {
    "connector": "loop_connector",
    "classifier": "loop_classifier",
    "extractor": "loop_extractor",
    "resolver": "loop_resolver",
    "pipeline": "loop_pipeline",
    "notifier": "loop_notifier",
    "nudge": "loop_nudge",
}


async def _run(name: str) -> None:
    configure_logging()
    dsn = os.environ["DATABASE_URL"]
    role = os.environ.get("DB_ROLE") or _ROLES.get(name)

    async with Database(dsn, role=role) as db:
        await until_signalled(_service(name, db))


def _service(name: str, db: Database) -> Service:
    """The service object, not its `run` — `until_signalled` needs `stop` too."""
    match name:
        case "connector":
            from loop.google.client import GoogleClient

            connector = ConnectorService(
                db,
                GoogleClient(
                    client_id=os.environ.get("GOOGLE_CLIENT_ID", ""),
                    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
                ),
                pubsub_topic=os.environ.get("GOOGLE_PUBSUB_TOPIC") or None,
            )
            return ConnectorRuntime(db, connector)
        case "classifier":
            return ClassifierService(db).consumer()
        case "extractor":
            return ExtractorService(db).consumer()
        case "resolver":
            # Concurrency one, by construction: ordering matters, and two
            # signals arriving together must not both decide there is no
            # application at this company yet.
            return ResolverService(db).consumer(ConsumerOptions(batch=1))
        case "pipeline":
            # `service`, not `consumer`: the funnel's materialised view is
            # refreshed by the thing that writes the rows under it, and the
            # last refresh is worth doing only once the consumer has stopped.
            return PipelineService(db).service(Queue.EVENT)
        case "notifier":
            return NotifierService(
                db,
                vapid=VapidConfig(
                    public_key=os.environ.get("VAPID_PUBLIC") or None,
                    private_key=os.environ.get("VAPID_PRIVATE") or None,
                    subject=os.environ.get("VAPID_SUBJECT", "mailto:loop@localhost"),
                ),
            ).consumer()
        case "nudge":
            return NudgeService(db)
        case other:
            raise SystemExit(f"no service called {other}")


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    name = sys.argv[1]
    if name == "api":
        # The API is served rather than looped, so it is uvicorn's to run.
        import uvicorn

        uvicorn.run(
            "loop.api:app_from_env",
            factory=True,
            host="0.0.0.0",
            port=int(os.environ.get("PORT", "3000")),
        )
        return
    asyncio.run(_run(name))


if __name__ == "__main__":
    main()
