"""MongoDB connection and index setup.

PyMongo is used directly rather than through an ODM. An ODM would re-declare
the shape of every document, which is the thing this storage choice exists to
avoid: the game writes JSON, that JSON is stored as-is, and a game update that
adds or renames a field costs nothing here.

What is still declared is the set of indexes, because those are integrity
constraints rather than shape. Uniqueness of a username, and one run per
(user, start_time), are guarantees the application relies on and MongoDB is
the only thing that can enforce them under concurrency.
"""

from __future__ import annotations

from flask import Flask, current_app
from pymongo import ASCENDING, MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ServerSelectionTimeoutError

# MongoClient owns a connection pool and is thread-safe, so one per process per
# URI is correct and creating one per request would be a bug. They are cached
# here rather than held on the app because the test suite builds a new app for
# every check, and eighteen connection pools to the same server is waste.
_clients: dict[tuple[str, int], MongoClient] = {}


def _client(uri: str, timeout_ms: int) -> MongoClient:
    key = (uri, timeout_ms)
    client = _clients.get(key)
    if client is None:
        client = MongoClient(
            uri,
            # Without this, BSON datetimes come back naive and every comparison
            # against models.now() raises. Note BSON stores milliseconds, so a
            # round trip truncates microseconds.
            tz_aware=True,
            # Defer the first connection to the first real operation, so that
            # importing or building an app never blocks on a down server.
            connect=False,
            # The driver's default is 30s, which turns a stopped container into
            # a long hang instead of a quick, obvious failure.
            serverSelectionTimeoutMS=timeout_ms,
        )
        _clients[key] = client
    return client


def init_app(app: Flask) -> None:
    """Attach this app's database handle.

    The handle lives on the app rather than in a module global so that several
    apps, each pointed at a different database, can exist in one process. That
    is what lets every test run against its own throwaway database.
    """
    client = _client(app.config["MONGODB_URI"], app.config["MONGODB_TIMEOUT_MS"])
    app.extensions["mongo"] = client[app.config["MONGODB_DB"]]


def get_db() -> Database:
    return current_app.extensions["mongo"]


def users() -> Collection:
    return get_db()["users"]


def runs() -> Collection:
    return get_db()["runs"]


def progress_snapshots() -> Collection:
    return get_db()["progress_snapshots"]


def card_stats() -> Collection:
    return get_db()["card_stats"]


def ensure_indexes() -> None:
    """Create the indexes the application depends on, and prove the server is up.

    create_index is idempotent, so this runs on every start. It is also the
    first operation to touch the server, which makes it the place where an
    unreachable database is reported, with an instruction rather than a
    driver traceback.
    """
    try:
        users().create_index(
            [("username", ASCENDING)], unique=True, name="uq_users_username"
        )
        # email is optional. A plain unique index would treat every account
        # without one as a duplicate of the first, so only documents that
        # actually carry a string are indexed.
        users().create_index(
            [("email", ASCENDING)],
            unique=True,
            name="uq_users_email",
            partialFilterExpression={"email": {"$type": "string"}},
        )
        # The game names each run file after its start time, so this is what
        # makes re-uploading a save folder a no-op. It also serves every query
        # that filters by owner.
        runs().create_index(
            [("user_id", ASCENDING), ("start_time", ASCENDING)],
            unique=True,
            name="uq_runs_user_start",
        )
        # Backs the per-tree counts, the run list and the character dropdown.
        runs().create_index(
            [("user_id", ASCENDING), ("is_modded", ASCENDING)],
            name="ix_runs_user_modded",
        )
        # At most one snapshot per user per save tree: a re-upload overwrites
        # rather than accumulating.
        progress_snapshots().create_index(
            [("user_id", ASCENDING), ("is_modded", ASCENDING)],
            unique=True,
            name="uq_progress_user_modded",
        )
        # One card stats sidecar per run. Keyed the same way as runs, but a
        # separate collection rather than a field on the run: the mod writes
        # the sidecar after every combat, so it usually arrives before the
        # .run file exists and must be storable without one.
        card_stats().create_index(
            [("user_id", ASCENDING), ("start_time", ASCENDING)],
            unique=True,
            name="uq_cardstats_user_start",
        )
    except ServerSelectionTimeoutError as exc:
        uri = current_app.config["MONGODB_URI"]
        raise RuntimeError(
            f"Cannot reach MongoDB at {uri}.\n"
            f"Start it with:\n"
            f"    docker compose up -d\n"
            f"or point MONGODB_URI at a server that is running."
        ) from exc
