# Slay the Spire 2 stats dashboard

Upload your save files and browse your runs.

[`docs/PLAN.md`](docs/PLAN.md) has the architecture, what each phase did and
why, the save-file domain knowledge worth not rediscovering, and what is
planned next. Start there.

# Running

```bash
cd ~/slay-the-spire-mod/dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python test_dashboard.py            # self-check, no game install needed

export FLASK_DEBUG=1                # local development
flask --app app run --debug         # http://127.0.0.1:5000
```

Register an account, then upload your save folder:

```
~/Library/Application Support/SlayTheSpire2/steam/<id>/profile1/saves
```

Selecting `steam/<id>` instead picks up every profile at once, vanilla and
modded, each tagged correctly. Uploading the same runs again is harmless; they
are matched on start time and skipped.

The database is SQLite at `instance/app.db`, created on first start.

## Schema changes

There are no migrations yet. `create_all()` only creates missing tables, so if
a column is added to a model the app refuses to start against an older
database and tells you what is missing. Delete it and upload again:

```bash
rm instance/app.db
```

Alembic becomes worth adding once a database holds data that cannot simply be
re-uploaded.

## Outside local development

`FLASK_DEBUG=1` falls back to a throwaway signing key. Anywhere else, set a real
one or the app refuses to start:

```bash
export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')
```

Serving over HTTPS also requires `SESSION_COOKIE_SECURE=1` so session cookies
are never sent in the clear. `DATABASE_URL` overrides the SQLite default when
moving to Postgres.

Still to do before exposing this publicly: TLS termination, a production WSGI
server, and rate limiting on the login and registration routes.

## Still to come

Character and card statistics derived from uploaded runs, plus a lifetime panel
from `progress.save` showing how many runs the game pruned before they could be
uploaded.
