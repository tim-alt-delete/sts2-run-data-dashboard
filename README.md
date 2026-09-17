# Running

```bash
cd ~/slay-the-spire-mod/dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python test_dashboard.py            # self-check, no game install needed

export FLASK_DEBUG=1                # local development
flask --app app run --debug         # http://127.0.0.1:5000
```

Register an account on the landing page. The database is SQLite at
`instance/app.db`, created on first start.

## Outside local development

`FLASK_DEBUG=1` falls back to a throwaway signing key. Anywhere else, set a real
one or the app refuses to start:

```bash
export SECRET_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')
```

Serving over HTTPS also requires `SESSION_COOKIE_SECURE=1` so session cookies
are never sent in the clear. `DATABASE_URL` overrides the SQLite default when
moving to Postgres.

## Still to come

Uploading save files, and per-user run pages built from them. Until then
`/local` reads the save folder on the machine running the server.
