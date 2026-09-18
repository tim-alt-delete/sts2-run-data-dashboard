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

docker compose up -d                # MongoDB on 127.0.0.1:27017

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

Data lives in the `sts2_dashboard` database, in the container's `mongo-data`
volume. `docker compose down` keeps it; `docker compose down -v` deletes it.

## Storage

Runs are stored as the game exported them. A `.run` file is JSON, so it goes
into MongoDB unchanged, alongside a handful of fields pulled out of it
(character, result, ascension and so on) that let the run list sort and filter
without opening the blob.

There are no migrations, and none are needed for the uploaded data: if a game
update adds, renames or removes a field, old and new documents simply differ
and both keep working. Only the extracted fields are the app's own invention,
and changing those means re-uploading, which is free because uploads are
idempotent.

What *is* declared is the set of indexes, in `db.py`. They are created on every
start and enforce the guarantees the app depends on: one account per username,
one run per (user, start time).

## Outside local development

`FLASK_DEBUG=1` falls back to a throwaway signing key. Anywhere else, set a real
one or the app refuses to start:

```bash
export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')
```

Serving over HTTPS also requires `SESSION_COOKIE_SECURE=1` so session cookies
are never sent in the clear. `MONGODB_URI` and `MONGODB_DB` point the app at a
different server or database; the compose file has no authentication and
publishes only on `127.0.0.1`, so anything beyond local development needs
credentials in the URI.

Still to do before exposing this publicly: TLS termination, a production WSGI
server, and rate limiting on the login and registration routes.

## Still to come

Character and card statistics derived from uploaded runs, plus a lifetime panel
from `progress.save` showing how many runs the game pruned before they could be
uploaded.
