"""What code is this server actually running?

A server that answers is not the same as a server that is current. For weeks a
container on :8001 served pre-#85 code against a schema this repo had long since
migrated; the live-API tests logged in fine and then failed on assertions nobody
could explain, because the thing under test was not the thing in the editor
(issue #93).

The cheapest honest answer to "what code is this?" is the set of migration files
the process carries. It changes on every schema-affecting commit, it needs no
build step and no git metadata at runtime, and it is directly comparable between
a caller's checkout and a server's - which is the whole point: `tests/conftest.py`
compares this against the working tree and refuses to run the live suites when
the two disagree.

Read from disk only. Nothing here touches the database, so `/api/health` stays
answerable when Postgres is down.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from typing import Any

from django.db.migrations.loader import MigrationLoader

#: Short enough to eyeball in a skip message, long enough that a collision is
#: not a thing that happens to anybody.
_DIGEST_LENGTH = 12


@lru_cache(maxsize=1)
def migration_names() -> tuple[str, ...]:
    """Every migration file this process's code carries, as ``app.name``, sorted.

    Cached: the files on disk cannot change under a running process, and a cold
    read walks every installed app.
    """
    # connection=None loads the migration files without asking the database what
    # it has applied - this is a fact about the *code*, not about any database.
    loader = MigrationLoader(None, ignore_no_migrations=True)
    return tuple(sorted(f"{app_label}.{name}" for app_label, name in loader.disk_migrations))


def migration_digest() -> str:
    return hashlib.sha256("\n".join(migration_names()).encode()).hexdigest()[:_DIGEST_LENGTH]


def migration_identity(*, include_names: bool) -> dict[str, Any]:
    """The identity payload ``/api/health`` publishes.

    ``include_names`` is off on hardened deployments: the digest and count are
    enough to *detect* a mismatch anywhere, while the names - which spell out
    every schema change ever made - are only worth publishing where the reader
    is a developer or CI, and where the endpoint is not open to the internet.
    """
    names = migration_names()
    identity: dict[str, Any] = {"count": len(names), "digest": migration_digest()}
    if include_names:
        identity["names"] = list(names)
    return identity
