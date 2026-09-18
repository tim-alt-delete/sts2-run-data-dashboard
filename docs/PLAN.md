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
docker compose up -d                # MongoDB; the self-check needs it too
python test_dashboard.py            # self-check, no game install needed
export FLASK_DEBUG=1
flask --app app run --debug         # http://127.0.0.1:5000
```

### Files

| File | Role |
|---|---|
| `app.py` | `create_app` factory, auth routes, per-user routes, upload ingest |
| `config.py` | env-driven config, `SECRET_KEY` guard, cookie flags |
| `db.py` | MongoDB client, per-app database handle, index setup |
| `models.py` | `User`, password hashing, validation, and every query |
| `uploads.py` | parsing and validation of untrusted uploaded files |
| `sts2data.py` | pure parsing of save data into DataFrames; no filesystem access |
| `test_dashboard.py` | the whole self-check suite, run directly, no pytest |
| `docker-compose.yml` | MongoDB for local development |
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
GET       /u/<username>/run/<id>     detail: path, deck, card utility, relics
GET/POST  /upload
```

Profiles are private. Someone else's page returns **404, not 403** — a 403
would confirm the account exists. Runs are scoped to their owner, so another
account cannot reach a run by guessing its id.

### Data storage

MongoDB, database `sts2_dashboard`, in a Docker volume. Four collections:

```
users               _id, username, password_hash, created_at, email?
runs                _id, user_id, start_time, is_modded, uploaded_at, data,
                    + character, result, killed_by, ascension, seed, build,
                      floors, run_time  (extracted at upload time)
progress_snapshots  _id, user_id, is_modded, uploaded_at, data
card_stats          _id, user_id, start_time, is_modded, uploaded_at, data,
                    + complete, seed, mod_version
```

`data` is the uploaded file exactly as the game wrote it. The extracted fields
beside it exist so the run list can sort and filter without loading a 12-83 KB
blob per row; they are always fetched with an explicit projection, because
without one MongoDB returns the blob too.

There is no declared schema. There **are** declared indexes, in `db.py`, since
uniqueness is an integrity guarantee rather than a shape:

| Collection | Index | Why |
|---|---|---|
| `users` | `username` unique | one account per name |
| `users` | `email` unique, **partial** on `{"email": {"$type": "string"}}` | email is optional; a plain unique index would make every account without one a duplicate of the first |
| `runs` | `(user_id, start_time)` unique | re-uploading a save folder is a no-op |
| `runs` | `(user_id, is_modded)` | per-tree counts, run list, character dropdown |
| `progress_snapshots` | `(user_id, is_modded)` unique | one snapshot per save tree |
| `card_stats` | `(user_id, start_time)` unique | one sidecar per run; a re-upload replaces it |

`dashboard/archive/` is an orphan: 8 `.run` files left over from the
pre-upload flow, gitignored, no longer read or written by any code path. Still
useful as a cold backup of runs the game may have since pruned, and the
verification data for every change (see Conventions).

### Why MongoDB

Phases 1-4 used SQLite through Flask-SQLAlchemy. The game exports JSON, so that
meant a JSON column SQLite stores as opaque TEXT: unqueryable, unindexable, and
recomputed in Python on every request. Meanwhile the declared columns needed a
migration story that did not exist — `create_all()` never alters a table, so
adding one meant `rm instance/app.db` and re-uploading.

Storing the export as documents removes the second problem outright: a game
update that adds or renames a field costs nothing, because nothing declares
what a run looks like. It also unblocks the first, since the run JSON is now
reachable from a query rather than only from Python.

What it cost:

- **A daemon.** SQLite was a file; MongoDB is a service, hence
  `docker-compose.yml` and a real server for the self-check.
- **Transactions.** A standalone mongod has no multi-document transactions, so
  `ingest()` is one `insert_many` rather than one commit and a batch is not
  atomic. Acceptable because uploading is idempotent: re-uploading repairs a
  partial batch.
- **Cascade delete.** `ON DELETE CASCADE` has no equivalent; deleting a user
  would have to delete their runs explicitly. No such code path exists yet.
  (SQLite was not enforcing it either — nothing set `PRAGMA foreign_keys=ON`.)

Not carried over: PyMongo is used directly rather than through an ODM, because
an ODM re-declares the shape of every document, which is the thing this change
exists to avoid.

Verified by rendering all 14 pages from the 8 real archived runs under both
backends and diffing: identical content. (Card table *row order* differs run to
run under both, which is a pre-existing wrinkle — see Future work.)

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
app with a throwaway database.

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

A `killed_by` field was added rather than derived, so the list page does not
have to load every run's JSON blob to render one cell. That exposed the
migration gap and prompted a startup schema check — both since removed by
phase 5, which is largely what motivated it.

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
the aggregates. Fine for hundreds of runs, wasteful for many thousands. See
[Cache the overview aggregates](#cache-the-overview-aggregates) under future
work.

### Phase 5 — MongoDB (done, `f846014`)

SQLite and Flask-SQLAlchemy replaced with MongoDB and PyMongo. Rationale and
costs are in [Why MongoDB](#why-mongodb) under Current state; the short version
is that the game exports JSON and SQLite could only store that as opaque text.

`models.py` no longer declares any table. It keeps `User` — Flask-Login needs
an object with an identity — plus password hashing, the username and password
rules, and every query in the app. `db.py` is new and holds the client and the
indexes. `app.py` lost `check_schema()` and its inner `tree_filter()`, and its
routes now call named functions instead of building queries inline.

`ingest()` batches into one `insert_many` instead of one commit, and reports
anything the unique index rejects as the duplicate it is, which covers two
uploads racing for the same run.

`uploads.py` gained a guard rejecting field names containing `.` or starting
with `$`. Nothing the game exports uses one today — 115 distinct keys across
the archived runs, none affected — but storing the export verbatim is the whole
point, and a game update is exactly what would introduce one.

Testing changed shape: MongoDB has no in-memory mode, so the self-check needs a
running server. Each check gets a uniquely-named database, dropped in a
`finally` so a failed run leaves nothing behind, and `main()` pings first so an
unreachable server prints an instruction instead of a driver traceback.

Verified by rendering all 14 pages from the 8 real archived runs and the real
`progress.save` under both backends and diffing: identical content. Separately
checked that all 8 run files and the 231-card `progress.save` round-trip
byte-for-byte, that `uploaded_at` and `created_at` come back timezone-aware,
and that both unique indexes are enforced by the server rather than only by the
application.

### Phase 6 — per-card utility (done, `7b25301`)

Ingests the sidecar file written by the **DataExporter mod**, which lives in the
sibling `DataExporter/` repo. See `DataExporter/docs/PLAN.md` for how the mod
collects it; this section only covers the dashboard side.

The problem it solves: `.run` files record the final deck but nothing about what
each card *did*. "Was this card good for this run?" cannot be answered from the
game's own exports at all, because per-card damage exists only in memory during
combat.

The mod writes `{start_time}.cardstats.json` into `saves/history/`, beside the
`.run` file, so an ordinary folder upload picks it up with no extra step.

- `uploads.py` gains `CARDSTATS_KIND` and `validate_cardstats`. The `.cardstats.json`
  suffix is tested **before** `.run` so a sidecar in the history folder is never
  mistaken for a run. Counters must be non-negative whole numbers: these are
  sums, so a negative one means the file is wrong, and clamping it to zero would
  hide that.
- `card_stats` is its own collection rather than a field on the run. The mod
  rewrites the file after every combat, so it routinely arrives **before** the
  run finishes and must be storable with no run to attach to. `save_card_stats`
  replaces rather than inserts, because the totals are cumulative and the newest
  file always supersedes the last.
- The join happens at render, not at ingest. A sidecar uploaded mid-run simply
  lights up later when its `.run` arrives.
- `sts2data.card_stats_table` joins to the final deck on `(card id, upgrade
  level)`, the same key the mod aggregates by, and adds per-play averages. Raw
  totals alone are not comparable: a Strike played twelve times and a Bash
  played twice are different questions.

Rows are sorted in Python, not pandas, deliberately — see [Card table row order
is not stable](#card-table-row-order-is-not-stable). Verified by rendering one
run 25 times and checking the order never moved.

**The honest limit.** Damage from Poison, Thorns and relic procs reaches the
game with no card attached, so the mod cannot attribute it. It goes to an
`unattributed` bucket which the run page reports as a share of total damage.
Without that, a damage-over-time deck would read as "my cards did nothing"
rather than "most of my damage is not attributable". Treat per-card damage as a
lower bound when that share is large.

Verified against the 8 real archived runs with a sidecar generated from each
run's real deck: 155 card rows, every one joined to a real deck entry, and the
per-run row count matching the number of distinct cards in that deck.

---

## 5. Future work

### Cache the overview aggregates

**Status: wanted. The current behaviour is a known, deliberate stopgap.**

`GET /u/<username>` rebuilds every statistic from scratch on each request. That
means loading the `data` JSON blob of every run the user owns, then recomputing
the totals, the character table and the card table, only to throw all of it
away when the page is rendered. A refresh redoes the lot.

At 8 runs this is imperceptible. The cost is linear in runs and each blob is
12-83 KB, so a user with the game's full 100-run history moves roughly 8 MB per
page view, and an account that has been archiving for a year would be far
worse. The card table is the expensive part: it walks every map point of every
run to count reward screens.

Options, roughly in order of effort:

1. **Cache the rendered aggregates per user**, keyed on something that changes
   when the data does — the user's run count plus the latest `uploaded_at` is
   enough, since runs are immutable once stored and only ever added. A plain
   in-process dict works for a single worker; Redis or a table if it ever runs
   multi-process. Invalidation is trivial because nothing mutates a stored run.
2. **Precompute on upload.** Ingest already touches every run it stores, so the
   per-run contribution to each aggregate could be derived there and kept in a
   summary collection. Turns page load into a cheap grouped query. More moving
   parts, but no longer blocked on migrations: a new collection or a new field
   on an existing document needs no schema change.
3. **Push the work into an aggregation pipeline.** Totals and most of the
   character table only need the extracted fields, so they are a single `$group`
   today. The card table needs the run JSON — but the JSON is now *in* the
   database rather than an opaque blob, so `$unwind` over `map_point_history`
   and `players.deck` can do the counting server-side instead of shipping every
   blob to Python.

Option 3 is the biggest win and the storage change is what unlocked it: under
SQLite the card table could not have been expressed this way at all. Option 1
is still the smallest change. Option 2 is the right end state and is no longer
gated on anything.

Worth doing when a page load becomes noticeable, or before opening the app to
other users, since the cost is per-user and concurrent.

### Card table row order is not stable

**Status: pre-existing, cosmetic, cheap to fix.**

`card_table` in `sts2data.py` iterates `set(won) | set(lost) | ...` and then
sorts with pandas' default non-stable quicksort. Rows that tie on both
`win_rate` and `runs` therefore come out in a different order on each process,
because Python randomises string hashing per process.

Caught while diffing old and new storage backends: the two differed, and then
the old backend differed from *itself* across two runs by the same amount. So
it is not a storage bug. Fix is `sorted(...)` over the union plus
`kind="stable"` in `sort_values`. Same for `relic_table` if it shares the
pattern.

### Database migrations

**Status: mostly moot, deliberately.**

Nothing declares the shape of a stored document, so a game update that adds,
renames or removes a field needs no migration: old and new documents differ and
both keep working. `check_schema()` and the `rm instance/app.db` ritual are
gone.

Two things could still need a data change:

- The fields extracted from each run at upload time (`character`, `result`, and
  so on) are the app's own invention. Changing how one is derived means
  rewriting it across existing documents, or re-uploading, which is free while
  uploads remain idempotent and the archive is intact.
- Indexes in `db.py` are created on every start. `create_index` is idempotent,
  but *removing* one is not — a dropped index has to be dropped by hand.

A real migration tool is worth it once documents hold anything derived and
stored rather than uploaded, since that is the point at which re-uploading
stops being a complete repair.

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

Deliberately deferred; none of it constrains the document shape or code shape.

- TLS termination
- production WSGI server (gunicorn)
- rate limiting on `/login` and `/register` (`Flask-Limiter`)
- `SESSION_COOKIE_SECURE=1`
- a MongoDB with authentication, via credentials in `MONGODB_URI`. The compose
  file has none and publishes only on `127.0.0.1`, which is fine for a laptop
  and not for anything else.
- backups. SQLite was one file to copy; this needs `mongodump` on a timer.

One trap worth recording: `MongoClient` must be created after a fork, and
`create_app()` runs in the worker, so plain gunicorn is fine but
`gunicorn --preload` would share a client across workers and misbehave.

Out of scope until asked: password reset and email verification. The optional
`email` field exists so this can be added later without chasing existing
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
  Author is `Timothy Pulliam <contact@timothypulliam.com>`, from the git
  identity configured globally in this environment.
- **Tests**: `python test_dashboard.py`, no pytest, no fixtures framework.
  Needs `docker compose up -d` first. Every check prints `name ok`. Current
  checks: parsing, lifetime totals, derived stats, card stats,
  derived==progress, auth, privacy, csrf, secret key, upload, upload form,
  upload folder, upload modded, upload privacy, run pages, overview page, run
  pages mod, run pages priv, card utility, card utility rej, card utility priv.
  Any config used to build an app must come from `test_config()`, or the
  database it creates is never dropped.
  Note `check_card_stats` and `check_card_utility` are unrelated despite the
  names: the first is the `card_stats` key inside `progress.save` (lifetime win
  rates per card), the second is the DataExporter sidecar (what a card did in
  one run).
- **Verification**: changes are checked against the 8 real archived runs, not
  only fixtures. Several real bugs were caught that way.
- **Phases**: built one at a time, each verified, committed and pushed, with a
  review pause between.
- **Security is never simplified away.** Input validation at trust boundaries,
  auth behaviour and upload limits are treated as requirements, not polish.
