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
