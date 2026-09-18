"""Configuration, read from the environment.

Nothing secret is committed. The only value that must be set for a real
deployment is SECRET_KEY, and the app refuses to start without it unless it is
running in debug or testing mode.
"""

from __future__ import annotations

import os

# A .run file is 12-83 KB in practice, and progress.save is larger but still
# small. The generous overall cap exists so a whole save folder (the game keeps
# up to 100 run files) can be uploaded in one request.
MAX_CONTENT_LENGTH = 64 * 1024 * 1024


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    """Base configuration. Values come from the environment."""

    SECRET_KEY = os.environ.get("SECRET_KEY")

    MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    MONGODB_DB = os.environ.get("MONGODB_DB", "sts2_dashboard")
    # Long enough to ride out a server that is still starting, short enough
    # that a server which is not running fails visibly instead of hanging.
    MONGODB_TIMEOUT_MS = int(os.environ.get("MONGODB_TIMEOUT_MS", "5000"))

    MAX_CONTENT_LENGTH = MAX_CONTENT_LENGTH

    # Cookies are never readable from JavaScript, and are not sent on
    # cross-site requests. Secure is off by default so http://127.0.0.1 works,
    # and must be switched on when the app is served over HTTPS.
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE")
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SECURE = _flag("SESSION_COOKIE_SECURE")


class TestConfig(Config):
    """Throwaway database, and CSRF disabled so tests can post plain forms.

    MongoDB has no in-memory mode, so the self-check needs a real server. Each
    test overrides MONGODB_DB with a unique name and drops it afterwards, which
    is what keeps checks isolated from each other and from real data.
    """

    TESTING = True
    SECRET_KEY = "test-secret-key"
    MONGODB_URI = os.environ.get(
        "MONGODB_TEST_URI", os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
    )
    MONGODB_DB = "sts2_dashboard_test"
    WTF_CSRF_ENABLED = False


def resolve(config: type[Config] | None = None) -> type[Config]:
    """Pick the configuration and fail loudly on an unusable SECRET_KEY.

    A missing key would otherwise silently produce a server that cannot keep
    anyone logged in, or worse, one running on a guessable key.
    """
    if config is not None:
        return config

    if not Config.SECRET_KEY:
        if _flag("FLASK_DEBUG"):
            Config.SECRET_KEY = "insecure-debug-key"
        else:
            raise RuntimeError(
                "SECRET_KEY is not set. Generate one with:\n"
                "    python -c 'import secrets; print(secrets.token_hex(32))'\n"
                "then export it, or set FLASK_DEBUG=1 for local development."
            )
    return Config
