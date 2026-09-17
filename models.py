"""Database models.

SQLAlchemy is used rather than raw sqlite3 so the eventual move to Postgres is
a connection-string change instead of a query rewrite.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()

USERNAME_PATTERN = re.compile(r"^[a-z0-9_-]{3,32}$")
MIN_PASSWORD_LENGTH = 8


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


class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    # Optional, and no mail is ever sent. It exists so password reset can be
    # added later without having to chase existing accounts for an address.
    email = db.Column(db.String(255), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=now)

    def set_password(self, password: str) -> None:
        """Hashed with scrypt, werkzeug's default. The plaintext is never stored."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password or "")

    @staticmethod
    def by_username(username: str) -> "User | None":
        return db.session.scalar(
            db.select(User).filter_by(username=normalize_username(username))
        )

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class Run(db.Model):
    """One finished run, as uploaded.

    The raw file is kept in `data` so every parser in sts2data keeps working
    unchanged, and so nothing is lost if more columns are wanted later. The
    other columns exist to sort and filter without opening the JSON.
    """

    __tablename__ = "runs"
    __table_args__ = (
        # The game names each file after its start_time, so re-uploading a save
        # folder is a no-op rather than a pile of duplicates.
        db.UniqueConstraint("user_id", "start_time", name="uq_runs_user_start"),
        db.Index("ix_runs_user_start", "user_id", "start_time"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    start_time = db.Column(db.Integer, nullable=False)

    character = db.Column(db.String(64))
    result = db.Column(db.String(16))
    killed_by = db.Column(db.String(64))
    ascension = db.Column(db.Integer)
    seed = db.Column(db.String(32))
    build = db.Column(db.String(32))
    floors = db.Column(db.Integer)
    run_time = db.Column(db.Integer)

    # A .run file carries no modded flag, so this comes from the upload: either
    # the folder path the browser reported, or the checkbox on the form.
    is_modded = db.Column(db.Boolean, nullable=False, default=False)

    data = db.Column(db.JSON, nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=now)

    user = db.relationship("User", backref=db.backref("runs", passive_deletes=True))

    def __repr__(self) -> str:
        return f"<Run {self.start_time} {self.character} {self.result}>"


class ProgressSnapshot(db.Model):
    """The latest progress.save for a user, kept per save tree.

    Runs are authoritative for every statistic. This exists to show lifetime
    totals, including runs the game has already pruned from its 100-file
    history, and so the gap between the two can be reported.
    """

    __tablename__ = "progress_snapshots"
    __table_args__ = (
        db.UniqueConstraint("user_id", "is_modded", name="uq_progress_user_modded"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    is_modded = db.Column(db.Boolean, nullable=False, default=False)
    data = db.Column(db.JSON, nullable=False)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=now)

    user = db.relationship(
        "User", backref=db.backref("progress_snapshots", passive_deletes=True)
    )

    def __repr__(self) -> str:
        return f"<ProgressSnapshot user={self.user_id} modded={self.is_modded}>"
