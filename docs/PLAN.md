# Slay the Spire 2 stats dashboard — plan and context

This file is the starting point for a new session. It records what exists, why
it is shaped the way it is, what is planned next, and the domain knowledge that
was expensive to work out. Read it instead of rediscovering all of this.

Repo: `dashboard/` is its own git repository (`tim-alt-delete/sts2-run-data`).
The parent `slay-the-spire-mod/` directory is not a git repo and also holds an
unrelated C# mod experiment (`DataExporter/`) and the decompiled game source.

---

## 1. What this is

A Flask web app for analysing Slay the Spire 2 runs. Users register, upload
their save files, and browse their run history.

The long-term goal is full statistical analysis: "with this card I win X% of
the time", "taking this relic led to a win Y% of the time", and eventually a
dataset good enough to train an AI to play the game. The per-floor decision
data needed for that is already being captured and stored.

**Current reality check:** 8 runs uploaded. Per-item win rates are statistically
meaningless at that sample size — 51 of 66 distinct relics appear exactly once.
The pipeline is being built now so the data accumulates; the aggregates become
trustworthy later. Anything that presents a rate should show its sample size.

---

## 2. Current state

### Running it

```bash
cd ~/slay-the-spire-mod/dashboard
source .venv/bin/activate
python test_dashboard.py            # self-check, no game install needed
export FLASK_DEBUG=1
flask --app app run --debug         # http://127.0.0.1:5000
```

### Files

| File | Role |
|---|---|
| `app.py` | `create_app` factory, auth routes, per-user routes, upload ingest |
| `config.py` | env-driven config, `SECRET_KEY` guard, cookie flags |
| `models.py` | `User`, `Run`, `ProgressSnapshot`, password hashing, validation |
| `uploads.py` | parsing and validation of untrusted uploaded files |
| `sts2data.py` | pure parsing of save data into DataFrames; no filesystem access |
| `test_dashboard.py` | the whole self-check suite, run directly, no pytest |
| `templates/` | `base`, `index`, `overview`, `runs`, `run`, `upload` |
| `docs/PLAN.md` | this file |

### Routes

```
GET/POST  /register  /login
POST      /logout
GET       /                          logged out: login + register
                                     logged in:  redirect to /u/<you>
GET       /u/<username>              overview
GET       /u/<username>/runs         list, filters: character, result, tree
GET       /u/<username>/run/<id>     detail: path, deck, relics
GET/POST  /upload
```

Profiles are private. Someone else's page returns **404, not 403** — a 403
would confirm the account exists. Runs are scoped to their owner, so another
account cannot reach a run by guessing its id.

### Data storage

Runs live **only** in the database, in `runs.data` (a JSON column) plus
extracted metadata columns for sorting and filtering. SQLite at
`instance/app.db`, gitignored.

`dashboard/archive/` is an orphan: 8 `.run` files left over from the
pre-upload flow, gitignored, no longer read or written by any code path. Still
useful as a cold backup of runs the game may have since pruned.

---

## 3. Domain knowledge

This is the part worth not rediscovering. Decompiled game source lives at
`../DataExporter/sts2-decompiled/sts2/` and is the authority for all of it.

### Save file layout (macOS)

```
~/Library/Application Support/SlayTheSpire2/{platform}/{userId}/[modded/]profile{N}/saves/
    progress.save           lifetime aggregates
    current_run.save        an IN-PROGRESS run, not a finished one
    current_run_mp.save     same, multiplayer
    prefs.save profile.save settings.save
    history/{start_time}.run    one per finished run
    history/*.backup *.corrupt  the game's own sidecars
```

Plain UTF-8 JSON, unencrypted, written temp-file-then-rename, so safe to read
while the game is running.

### Sharp edges

- **The game prunes run history at 100 files / 5 MB.** Older runs are gone
  permanently. `progress.save` still counts them, so the gap between the two is
  a real measure of lost data.
- **`current_run.save` looks like a finished run.** It has `start_time`,
  `players` and `map_point_history`, and no `win` field. Anything defaulting
  `win` to `False` will store an active run as a completed loss. Uploads
  classify by filename before parsing, specifically to exclude it.
- **Loading any mod switches the game to a separate `modded/` tree** with a
  fresh default profile (`UserDataPathProvider.cs:43`). Modded runs never touch
  vanilla stats. A `.run` file carries **no** modded flag internally.
- **`map_point_history[act][point].rooms` can hold more than one room** — an
  event that leads into a fight records both. Indexing `rooms[0]` silently
  loses the combat.
- **Rest sites, treasure rooms and shops have no `model_id`,** only a
  `room_type`. A blank room name for those is correct.
- **`acts` always lists all three acts** even for a run that died in act 1.
  Index it by position; never zip it against `map_point_history`.
- **`player_stats` keys are open-ended.** The game omits properties still at
  their default. Always use `.get()`.
- **`ModelId` serializes as `"CATEGORY.ENTRY"`**, e.g. `CHARACTER.IRONCLAD`.
  Enums are snake_case strings.
- **All times are seconds** (Unix timestamp deltas). `fastest_win_time == -1`
  means no win yet.
- **`killed_by` is inconsistent inside the game itself.**
  `RunHistoryUtilities` reads `Rooms.First()` while `ProgressSaveManager` reads
  `Rooms.Last()`. So dying in an event-triggered fight records
  `killed_by_event`, not `killed_by_encounter`.

### Three overlaps that cause double-reporting

Verified across all 8 real runs. Each must be subtracted, not summed:

1. **Every picked card also appears in `cards_gained`.** Subtract picked to
   leave only cards granted without a choice (events, ancients, shop buys).
2. **Every shop purchase also appears in `relic_choices`/`potion_choices`** as
   picked. Render the purchase, suppress the duplicate pick.
3. **An ancient's chosen relic is repeated** by an `event_choices` entry whose
   `title.table` is `"relics"` (19/19 cases). Show the relic once as a gain and
   list only the *declined* options.

Also collapse genuine repeats: upgrading two copies of a card records two
identical entries.

### Event choice keys

`title.table` discriminates two shapes:
- `"events"` → `NAME.pages.PAGE.options.OPTION.title`, take the segment after
  `options` (34/34 cases)
- `"relics"` → `RELIC_NAME.title`, only at ancients (19/19 cases)

### Browser folder uploads

`webkitRelativePath` is relative to **the folder the user picked, including
that folder's own name**. Consequences:

| Selected | Paths sent | `modded/` visible? |
|---|---|---|
| `steam/<id>/` | `<id>/modded/profile1/saves/history/x.run` | yes |
| `.../modded/profile1/saves` | `saves/history/x.run` | **no** |

This is why the modded checkbox exists. It is not a fallback for single-file
selection; it is the only way to tag a `saves` folder chosen from inside
`modded/`. `webkitdirectory` also cannot be combined with ordinary file
selection on one input.

### Verified aggregate formulas

From `ProgressSaveManager.UpdateWithRunData`. These let run files reproduce
`progress.save` exactly, which is what makes phase 4 possible:

- `floors_climbed` += number of map points in the run
- `total_playtime` += `run_time`
- `total_wins` / `total_losses` per character; daily and custom runs excluded
- **abandons count as losses** in `progress.save`; run files keep them distinct
- `current_streak` resets to 0 on a loss; `best_win_streak` is its high-water mark
- `fastest_win_time` = smallest `run_time` among wins
- `max_ascension` is **incremented by a win**, so winning at Asc 0 sets it to 1.
  It means "highest unlocked", not "highest played"
- `card_stats.times_won` / `times_lost`: once per **distinct card id in the
  final deck**, per run. Not per pick. A card picked and later removed counts
  for nothing
- `card_stats.times_picked` / `times_skipped`: one per `card_choices` entry

### Validation results (recorded 8-run dataset)

A full cross-check was run and everything agreed:

- **24/24** `progress.save` fields matched values recomputed from run files,
  including playtime to the second
- Deck conservation `len(deck) − gained + removed` gives a consistent starting
  deck size per character: Ironclad 10, Silent 12, Regent 10
- Exactly **one** starting relic implied per run, 8 for 8
- `killed_by` matched `rooms[0].model_id` on all 6 losses
- `start_time + run_time` matched file mtime within a second on 6 of 8 runs;
  the two outliers were `+93s` and `+57s`, both positive, consistent with
  end-of-run animations

---

## 4. Phases

### Phase 1 — user accounts (done, `a7776ee`)

Database, `User` model, register/login/logout, landing page.

Security decided up front because retrofitting is awkward: scrypt hashing via
werkzeug (already a Flask dependency), `SECRET_KEY` from the environment with
the app refusing to start without one outside debug, app-wide CSRF, `HttpOnly`
+ `SameSite=Lax` cookies with `Secure` behind an env flag, byte-identical
responses for wrong-password and unknown-account, and `?next=` restricted to
same-site paths.

`app.py` became a `create_app` factory. Flask's CLI discovers it automatically,
and importing the module has no side effects, which is what lets tests build an
app with an in-memory database.

### Phase 2 — uploads (done, `ce1a57e`, `4da3a4a`, `3cac72c`)

`Run` and `ProgressSnapshot` models, `/upload`.

Uploads are treated as hostile: size caps checked while reading, UTF-8 and JSON
failures reported rather than raised, `RecursionError` caught, structure
validated before any field is read, and filenames used only for display and
modded detection — never to touch the filesystem.

Dedupe is `UNIQUE(user_id, start_time)`, so re-uploading a whole save folder is
a no-op. The same run appearing twice within one request is handled too.

Two follow-up fixes: the file input had no `webkitdirectory` so folders could
not be selected at all, and that fix exposed `current_run.save` being storable
as a finished loss. Individual-file upload was then removed as redundant.

### Phase 3 — per-user run pages (done, `be7e357`)

`/u/<username>/runs` and `/u/<username>/run/<id>` read the database. The
local-file routes were removed, and `sts2data.py` lost everything touching the
filesystem — it now takes an already-decoded dict and does not care where it
came from. Net −241 lines.

A `killed_by` column was added rather than derived, so the list page does not
have to load every run's JSON blob to render one cell. That exposed the
migration gap and prompted the startup schema check (see below).

Modded runs are hidden by default, with filters to isolate or combine them.

### Phase 4 — overview statistics (done, `7da6488`)

`totals`, `character_table` and `card_table` are rebuilt from **uploaded runs**
using the formulas above, replaying them in play order so streaks and the
ascension ladder accumulate exactly as the game does. The `progress.save`
versions were deleted rather than kept alongside: two implementations of the
same numbers would drift, and relic stats exist only in run files anyway.

`progress.save` now supplies only `lifetime_totals()` and the pruning gap, so
the overview can say "your save reports 140 runs, 40 of them have no run file".

Verified against the real 8-run dataset: every derived figure matches what
`progress.save` reported, including playtime to the second and the ascension
ladder reading 1 for characters that won at Ascension 0.

`check_derived_matches_progress` pins this down permanently: it builds runs and
the `progress.save` the game would have written for them, and asserts the two
agree field by field, including that abandons fold into losses on the
`progress.save` side only.

Known ceiling: the overview loads every run's JSON on each request to rebuild
the aggregates. Fine for hundreds of runs, wasteful for many thousands. Cache
or precompute when that matters.

---

## 5. Future work

### Database migrations

**Status: deferred, has a known trap.**

`db.create_all()` creates missing tables but never alters existing ones. A
column added to a model is silently absent until the table is rebuilt, and the
first query dies with a bare `no such column`.

`check_schema()` in `app.py` compares each model against the real table at
startup and raises an actionable error naming the missing columns. The current
fix is `rm instance/app.db` and re-upload, which is acceptable only because
uploads are idempotent and the data is small.

Alembic becomes worth adding when a database holds data that cannot simply be
re-uploaded — realistically once there is more than one user, or once anything
is derived and stored rather than uploaded. At that point `check_schema()` can
either go away or become a "migrations pending" check.

### pandas

**Status: kept deliberately, currently underused.**

Every present use is "build a DataFrame from a list of dicts, sort it, render
HTML": `run_path_table`, `deck_table`, `relic_table`, `character_table`,
`card_table`, plus `pd.isna` in assertions. `collections.Counter` and a Jinja
loop would cover all of it, and `.to_html()` is the reason the deck, relic,
character and card tables cannot be styled per-cell the way the runs table is.

Phase 4 did not change this. The aggregation itself is `Counter` and plain
dict work; pandas only formats the result at the end.

It stays because the actual analytical work ahead — relic and card cross-tabs,
deck clustering, exporting a training set — is real DataFrame work. Phase 4's
aggregates are `Counter`-shaped either way, so this blocks nothing.

Revisit if: a page needs per-cell styling that `.to_html()` cannot express, for
instance colouring win rates the way the runs table colours results. Removing it would drop
a large dependency; keeping it costs nothing but import time.

### Deployment

Deliberately deferred; none of it constrains the schema or code shape.

- TLS termination
- production WSGI server (gunicorn)
- rate limiting on `/login` and `/register` (`Flask-Limiter`)
- `SESSION_COOKIE_SECURE=1`
- Postgres via `DATABASE_URL`; `JSON` columns map to `jsonb`

Out of scope until asked: password reset and email verification. The optional
`email` column exists so this can be added later without chasing existing
accounts.

### Other ideas raised but not scheduled

- `.zip` upload (needs bomb protection: uncompressed-size and member-count caps)
- automated archiving on a timer, so runs cannot be pruned between visits
- a run-decisions export (one row per choice, joined to run outcome) as the
  foundation for analysis and AI training
- richer run detail: event outcomes are currently shown by option name only

---

## 6. Conventions

- **Commits**: lowercase conventional style (`feat:`, `fix:`, `refactor:`).
  Body explains reasoning and any non-obvious constraint discovered.
  Author is `Timothy Pulliam <contact@timothypulliam.com>`; there is no git
  identity configured in the sandbox, so it is passed per-commit via
  `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` env vars.
- **Tests**: `python test_dashboard.py`, no pytest, no fixtures framework.
  Every check prints `name ok`. Current checks: parsing, lifetime totals,
  derived stats, card stats, derived==progress, auth, privacy, csrf, secret
  key, upload, upload form, upload folder, upload modded, upload privacy, run
  pages, overview page, run pages mod, run pages priv.
- **Verification**: changes are checked against the 8 real archived runs, not
  only fixtures. Several real bugs were caught that way.
- **Phases**: built one at a time, each verified, committed and pushed, with a
  review pause between.
- **Security is never simplified away.** Input validation at trust boundaries,
  auth behaviour and upload limits are treated as requirements, not polish.
