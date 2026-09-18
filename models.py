"""Documents and the queries over them.

MongoDB is used rather than a relational store because the game already exports
JSON. A run is kept exactly as the game wrote it, so a game update that adds,
renames or removes a field needs no migration and loses nothing.

Only User gets a class, because Flask-Login needs an object with an identity.
Runs and progress snapshots are plain dicts: the same dicts uploads.py
validated and sts2data.py knows how to read. Every query lives here rather
than in the routes, so there is one place to look when the document shape or
an index changes.

Document shapes, for reference rather than enforcement:

    users               _id, username, password_hash, created_at, email?
    runs                _id, user_id, start_time, is_modded, uploaded_at, data,
                        and the extracted fields in RUN_LIST_FIELDS
    progress_snapshots  _id, user_id, is_modded, uploaded_at, data
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from flask_login import UserMixin
from pymongo import DESCENDING
from pymongo.errors import BulkWriteError
from werkzeug.security import check_password_hash, generate_password_hash

import db

USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{3,32}$")
MIN_PASSWORD_LENGTH = 8

# The fields extracted from a run at upload time, so the run list can sort and
# filter without opening the JSON blob. They are always projected explicitly:
# without a projection MongoDB would ship every `data` blob to render a table
# that never looks at one.
RUN_LIST_FIELDS = (
    "start_time",
    "character",
    "ascension",
    "result",
    "killed_by",
    "build",
    "floors",
    "seed",
    "is_modded",
)

DUPLICATE_KEY = 11000


def now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_username(username: str) -> str:
    """Usernames are compared case-insensitively, so they are stored lowercased."""
    return (username or "").strip().lower()


def username_error(username: str) -> str | None:
    """Why this username is unusable, or None if it is fine."""
    if not username:
        return "Choose a username."
    if not USERNAME_PATTERN.fullmatch(username):
        return (
            "Usernames are 3 to 32 characters, using lowercase letters, "
            "numbers, hyphens and underscores."
        )
    return None


def password_error(password: str) -> str | None:
    """Why this password is unusable, or None if it is fine."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"Passwords must be at least {MIN_PASSWORD_LENGTH} characters."
    return None


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------


class User(UserMixin):
    """An account, wrapping its document.

    The document's `_id` is an ObjectId and never appears in a URL: pages are
    addressed by username, and runs by their start time. It only travels in the
    session cookie, via get_id().
    """

    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        self.id = doc["_id"]
        self.username = doc["username"]
        # Optional, and no mail is ever sent. It exists so password reset can be
        # added later without having to chase existing accounts for an address.
        self.email = doc.get("email")
        self.password_hash = doc["password_hash"]
        self.created_at = doc.get("created_at")

    def get_id(self) -> str:
        """Flask-Login keeps this in the session and hands it back as a string."""
        return str(self.id)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password or "")

    @staticmethod
    def by_username(username: str) -> "User | None":
        doc = db.users().find_one({"username": normalize_username(username)})
        return User(doc) if doc else None

    @staticmethod
    def by_id(user_id: str) -> "User | None":
        """Look up by the string Flask-Login took from the session.

        The value is attacker-controlled in the sense that a tampered cookie
        can carry anything, so a malformed id is a miss rather than a crash.
        """
        try:
            oid = ObjectId(user_id)
        except (InvalidId, TypeError):
            return None
        doc = db.users().find_one({"_id": oid})
        return User(doc) if doc else None

    def __repr__(self) -> str:
        return f"<User {self.username}>"


def create_user(username: str, email: str | None, password: str) -> User:
    """Register an account. The plaintext password is never stored."""
    doc: dict[str, Any] = {
        "username": normalize_username(username),
        # scrypt, werkzeug's default.
        "password_hash": generate_password_hash(password),
        "created_at": now(),
    }
    # Absent, not null. The unique index on email only covers documents whose
    # email is a string, so storing an explicit null for every account without
    # one would make the second such account a duplicate of the first.
    if email:
        doc["email"] = email
    result = db.users().insert_one(doc)
    doc.setdefault("_id", result.inserted_id)
    return User(doc)


def email_taken(email: str) -> bool:
    return db.users().find_one({"email": email}, {"_id": 1}) is not None


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


def tree_query(tree: str) -> dict[str, Any]:
    """Modded runs are kept out of the numbers unless asked for."""
    if tree == "modded":
        return {"is_modded": True}
    if tree == "all":
        return {}
    return {"is_modded": False}


def run_counts_by_tree(user_id: ObjectId) -> dict[bool, int]:
    """How many runs the user has in each save tree."""
    runs = db.runs()
    return {
        False: runs.count_documents({"user_id": user_id, "is_modded": False}),
        True: runs.count_documents({"user_id": user_id, "is_modded": True}),
    }


def run_data_for_user(user_id: ObjectId, tree: str) -> list[dict]:
    """Every raw run file for a tree, for the aggregates to be rebuilt from.

    This is the expensive query: it loads one 12-83 KB blob per run on each
    overview render. Fine for hundreds of runs, wasteful for many thousands.
    See the overview caching note in docs/PLAN.md.
    """
    cursor = db.runs().find(
        {"user_id": user_id, **tree_query(tree)}, {"data": 1, "_id": 0}
    )
    return [doc["data"] for doc in cursor]


def run_rows(
    user_id: ObjectId, tree: str, character: str = "", result: str = ""
) -> list[dict]:
    """The run list, newest first, without touching any JSON blob."""
    query: dict[str, Any] = {"user_id": user_id, **tree_query(tree)}
    if character:
        query["character"] = character
    if result:
        query["result"] = result

    projection: dict[str, Any] = {field: 1 for field in RUN_LIST_FIELDS}
    projection["_id"] = 0
    return list(db.runs().find(query, projection).sort("start_time", DESCENDING))


def distinct_characters(user_id: ObjectId, tree: str) -> list[str]:
    """Characters the user has played, for the filter dropdown."""
    values = db.runs().distinct("character", {"user_id": user_id, **tree_query(tree)})
    return sorted(c for c in values if c)


def find_run(user_id: ObjectId, start_time: int) -> dict | None:
    """One run, scoped to its owner so a run cannot be reached by guessing an id."""
    return db.runs().find_one({"user_id": user_id, "start_time": start_time})


def existing_start_times(user_id: ObjectId) -> set[int]:
    """Which runs the user already has, so a re-upload can be recognised."""
    return set(db.runs().distinct("start_time", {"user_id": user_id}))


def run_document(
    user_id: ObjectId, is_modded: bool, data: dict, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Build a run document from a validated upload."""
    return {
        "user_id": user_id,
        "is_modded": is_modded,
        "uploaded_at": now(),
        "data": data,
        **metadata,
    }


def insert_runs(docs: list[dict]) -> set[int]:
    """Insert prepared run documents, reporting any the database already had.

    Unordered so one rejected document does not abandon the rest. The caller
    has already filtered out runs it knows about; this catches the case where
    the same run arrives from two uploads at once, which the unique index on
    (user_id, start_time) turns into a duplicate-key error rather than a
    second copy.
    """
    if not docs:
        return set()
    try:
        db.runs().insert_many(docs, ordered=False)
    except BulkWriteError as exc:
        duplicates = set()
        for error in exc.details.get("writeErrors", []):
            if error.get("code") != DUPLICATE_KEY:
                # Anything other than "already there" is a real failure.
                raise
            duplicates.add(error["op"]["start_time"])
        return duplicates
    return set()


# --------------------------------------------------------------------------
# progress snapshots
# --------------------------------------------------------------------------


def progress_snapshot(user_id: ObjectId, is_modded: bool) -> dict | None:
    """The latest progress.save for a save tree, or None.

    Runs are authoritative for every statistic. This exists to show lifetime
    totals, including runs the game has already pruned from its 100-file
    history, and so the gap between the two can be reported.
    """
    return db.progress_snapshots().find_one(
        {"user_id": user_id, "is_modded": is_modded}
    )


def save_progress_snapshot(user_id: ObjectId, is_modded: bool, data: dict) -> None:
    """Store the snapshot for a save tree, replacing any earlier one."""
    key = {"user_id": user_id, "is_modded": is_modded}
    db.progress_snapshots().replace_one(
        key, {**key, "data": data, "uploaded_at": now()}, upsert=True
    )
