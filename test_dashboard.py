"""Self-check for the dashboard.

Runs without the game installed, against a synthetic save tree and an
in-memory database, so you can confirm the install works before pointing it at
real save data.

    python test_dashboard.py

Fixture key names are taken from the decompiled game source:
SerializableProgress.cs, CharacterStats.cs, CardStats.cs, UserDataPathProvider.cs
"""

import io
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

import app as dashboard
import sts2data
import uploads
from config import TestConfig
from models import ProgressSnapshot, Run, User, db

PASSWORD = "correct-horse-battery"


def make_client(username: str | None = "tim"):
    """A test client, optionally already registered and logged in."""
    app = dashboard.create_app(TestConfig)
    client = app.test_client()
    if username:
        client.post("/register", data={"username": username, "password": PASSWORD})
    return app, client

USER = "76561198182361854"

VANILLA_PROGRESS = {
    "schema_version": 3,
    "unique_id": "abc",
    "total_playtime": 45296,
    "floors_climbed": 611,
    "character_stats": [
        {"id": "CHARACTER.IRONCLAD", "max_ascension": 4, "total_wins": 3, "total_losses": 7,
         "fastest_win_time": 3725, "best_win_streak": 2, "current_streak": 1, "playtime": 20000},
        {"id": "CHARACTER.SILENT", "max_ascension": 2, "total_wins": 0, "total_losses": 5,
         "fastest_win_time": -1, "best_win_streak": 0, "current_streak": 0, "playtime": 9000},
        # No finished runs: exercises the divide-by-zero guard.
        {"id": "CHARACTER.DEFECT", "max_ascension": 0, "total_wins": 0, "total_losses": 0,
         "fastest_win_time": -1, "best_win_streak": 0, "current_streak": 0, "playtime": 0},
    ],
    "card_stats": [
        {"id": "CARD.STRIKE", "times_picked": 40, "times_skipped": 60, "times_won": 3, "times_lost": 12},
        {"id": "CARD.OFFERING", "times_picked": 9, "times_skipped": 1, "times_won": 3, "times_lost": 3},
        # One run, 100% win rate: must be filtered out, else it tops the sort.
        {"id": "CARD.LUCKY_ONCE", "times_picked": 1, "times_skipped": 0, "times_won": 1, "times_lost": 0},
        # Absent keys: the game omits properties still at their default value.
        {"id": "CARD.NEVER_SEEN"},
    ],
}

# A freshly created modded profile is ProgressState.CreateDefault() with no history dir.
MODDED_PROGRESS = {"schema_version": 3, "unique_id": "def", "character_stats": [], "card_stats": []}

# Real .run schema (start_time, players[].character, ascension, win, was_abandoned,
# killed_by_encounter/event, build_id, map_point_history). Filenames match start_time,
# as the game names them. Covers all three Result values across three characters.
RUN_FILES = {
    "1789424859.run": {  # oldest: a win, nothing killed it
        "start_time": 1789424859, "ascension": 4, "win": True, "was_abandoned": False,
        "build_id": "v0.107.1", "killed_by_encounter": "NONE.NONE", "killed_by_event": "NONE.NONE",
        "seed": "3J6ZXDRGZE", "game_mode": "standard", "run_time": 3725,
        "acts": ["ACT.OVERGROWTH", "ACT.HIVE", "ACT.GLORY"],
        "players": [{
            "character": "CHARACTER.IRONCLAD",
            "badges": [{"id": "ELITE", "rarity": "bronze"}],
            "deck": [
                {"id": "CARD.STRIKE_IRONCLAD", "floor_added_to_deck": 1},
                {"id": "CARD.STRIKE_IRONCLAD", "floor_added_to_deck": 1},
                {"id": "CARD.BASH", "floor_added_to_deck": 1, "current_upgrade_level": 1},
                {"id": "CARD.STOMP", "floor_added_to_deck": 3,
                 "enchantment": {"id": "ENCHANTMENT.INSTINCT", "amount": 1}},
            ],
            "relics": [
                {"id": "RELIC.VAJRA", "floor_added_to_deck": 3},
                {"id": "RELIC.BYRDPIP", "floor_added_to_deck": 2},
                {"id": "RELIC.BURNING_BLOOD", "floor_added_to_deck": 1},
            ],
        }],
        "map_point_history": [
            [
                # Monster floor with a card reward: one taken, two skipped.
                {"map_point_type": "monster",
                 "rooms": [{"model_id": "ENCOUNTER.NIBBITS_WEAK", "room_type": "monster",
                            "monster_ids": ["MONSTER.NIBBIT", "MONSTER.NIBBIT"], "turns_taken": 3}],
                 "player_stats": [{"player_id": 1, "current_hp": 78, "max_hp": 80, "current_gold": 119,
                                   "cards_gained": [{"id": "CARD.SETUP_STRIKE"}],
                                   "card_choices": [
                                       {"card": {"id": "CARD.SETUP_STRIKE"}, "was_picked": True},
                                       {"card": {"id": "CARD.TREMBLE"}, "was_picked": False},
                                       {"card": {"id": "CARD.BLOOD_WALL"}, "was_picked": False}]}]},
                # Rest site: no model_id, only a room_type. Hatching an egg here
                # transforms a card and grants a relic, so a relic's floor does
                # not have to be an obvious relic room.
                {"map_point_type": "rest_site",
                 "rooms": [{"room_type": "rest_site", "turns_taken": 0}],
                 "player_stats": [{"player_id": 1, "current_hp": 80, "max_hp": 80, "current_gold": 119,
                                   "rest_site_choices": ["HATCH"],
                                   "cards_transformed": [{
                                       "original_card": {"id": "CARD.BYRDONIS_EGG", "floor_added_to_deck": 1},
                                       "final_card": {"id": "CARD.BYRD_SWOOP", "floor_added_to_deck": 2}}],
                                   "relic_choices": [
                                       {"choice": "RELIC.BYRDPIP", "was_picked": True}]}]},
            ],
            [
                # Event that leads into a fight: two rooms on one map point.
                {"map_point_type": "unknown",
                 "rooms": [
                     {"model_id": "EVENT.DENSE_VEGETATION", "room_type": "event", "turns_taken": 0},
                     {"model_id": "ENCOUNTER.DENSE_VEGETATION_EVENT_ENCOUNTER", "room_type": "monster",
                      "monster_ids": ["MONSTER.WRIGGLER"] * 4, "turns_taken": 8}],
                 "player_stats": [{"player_id": 1, "current_hp": 61, "max_hp": 80, "current_gold": 150,
                                   "event_choices": [{"title": {
                                       "key": "DENSE_VEGETATION.pages.INITIAL.options.REST.title",
                                       "table": "events"}}]}]},
                # Shop purchase.
                {"map_point_type": "shop",
                 "rooms": [{"room_type": "shop", "turns_taken": 0}],
                 "player_stats": [{"player_id": 1, "current_hp": 61, "max_hp": 80, "current_gold": 20,
                                   "bought_relics": ["RELIC.MINIATURE_TENT"],
                                   "relic_choices": [
                                       {"choice": "RELIC.MINIATURE_TENT", "was_picked": True}]}]},
                # Ancient: the chosen option is also a picked relic, and the
                # event_choices entry repeats its name with table 'relics'.
                {"map_point_type": "ancient",
                 "rooms": [{"model_id": "EVENT.TEZCATARA", "room_type": "event", "turns_taken": 0}],
                 "player_stats": [{"player_id": 1, "current_hp": 61, "max_hp": 80, "current_gold": 20,
                                   "event_choices": [{"title": {
                                       "key": "YUMMY_COOKIE.title", "table": "relics"}}],
                                   "ancient_choice": [
                                       {"TextKey": "YUMMY_COOKIE", "was_chosen": True},
                                       {"TextKey": "STORYBOOK", "was_chosen": False}],
                                   "relic_choices": [
                                       {"choice": "RELIC.YUMMY_COOKIE", "was_picked": True}],
                                   "upgraded_cards": ["CARD.DEFEND_IRONCLAD", "CARD.DEFEND_IRONCLAD"]}]},
            ],
        ],
    },
    "1789508732.run": {  # middle: a loss to a named encounter
        "start_time": 1789508732, "ascension": 0, "win": False, "was_abandoned": False,
        "build_id": "v0.107.1", "killed_by_encounter": "OWL_MAGISTRATE_NORMAL", "killed_by_event": "NONE.NONE",
        "seed": "0VD3JRH6FY", "game_mode": "standard", "run_time": 4363,
        "acts": ["ACT.OVERGROWTH", "ACT.HIVE", "ACT.GLORY"],
        "players": [{"character": "CHARACTER.SILENT"}],
        "map_point_history": [[{}, {}, {}, {}, {}, {}]],  # 6 floors
    },
    "1789515832.run": {  # newest: quit mid-run
        "start_time": 1789515832, "ascension": 1, "win": False, "was_abandoned": True,
        "build_id": "v0.107.2", "killed_by_encounter": "NONE.NONE", "killed_by_event": "NONE.NONE",
        "seed": "QLDTDQLGQY", "game_mode": "standard", "run_time": 1096,
        "acts": ["ACT.UNDERDOCKS", "ACT.HIVE", "ACT.GLORY"],
        "players": [{"character": "CHARACTER.DEFECT"}],
        "map_point_history": [[{}, {}]],  # 2 floors
    },
}


def build_fixture(root: Path) -> None:
    vanilla = root / "steam" / USER / "profile1" / "saves"
    modded = root / "steam" / USER / "modded" / "profile1" / "saves"
    (vanilla / "history").mkdir(parents=True)
    modded.mkdir(parents=True)

    (vanilla / "progress.save").write_text(json.dumps(VANILLA_PROGRESS), encoding="utf-8")
    (modded / "progress.save").write_text(json.dumps(MODDED_PROGRESS), encoding="utf-8")

    for name, content in RUN_FILES.items():
        (vanilla / "history" / name).write_text(json.dumps(content), encoding="utf-8")
    # The game's own sidecar files, which must be ignored.
    (vanilla / "history" / "1789424859.run.backup").write_text("{}", encoding="utf-8")
    (vanilla / "history" / "1789111111.corrupt").write_text("{}", encoding="utf-8")


def check_loader(archive: Path) -> None:
    profiles = sts2data.find_profiles()
    assert len(profiles) == 2, profiles
    assert [p["is_modded"] for p in profiles] == [False, True]
    assert profiles[0]["runs"] == 15 and profiles[0]["run_files"] == 3
    assert profiles[1]["runs"] == 0

    profile = sts2data.default_profile(profiles)
    assert not profile["is_modded"], "must default to vanilla, not the newer empty modded tree"

    progress = sts2data.load_progress(profile["saves"])
    assert sts2data.totals(progress) == {
        "runs": 15, "wins": 3, "losses": 12, "win_rate": 20.0,
        "playtime": "12:34:56", "floors_climbed": 611,
    }

    characters = sts2data.character_table(progress)
    assert list(characters["character"]) == ["IRONCLAD", "SILENT", "DEFECT"], "sorted by runs"
    assert characters.loc[0, "win_rate"] == 30.0
    assert characters.loc[0, "fastest_win"] == "1:02:05", "fastest_win_time is seconds"
    assert pd.isna(characters.loc[1, "fastest_win"]), "-1 means no win yet"
    assert pd.isna(characters.loc[2, "win_rate"]), "zero runs must not divide by zero"

    cards = sts2data.card_table(progress, min_runs=5)
    assert list(cards["card"]) == ["OFFERING", "STRIKE"], list(cards["card"])
    assert "LUCKY_ONCE" not in list(cards["card"]), "1-run card must be filtered"
    assert cards.loc[0, "win_rate"] == 50.0 and cards.loc[0, "pick_rate"] == 90.0
    assert sts2data.card_table(progress, min_runs=0).shape[0] == 4, "absent keys default to 0"

    assert sts2data.data_loss_report(progress, profile["saves"]) == {
        "recorded": 15, "on_disk": 3, "missing": 12,
    }

    assert sts2data.archive_runs(profile["saves"], profile["label"]) == 3
    assert sts2data.archive_runs(profile["saves"], profile["label"]) == 0, "must not recopy"
    copied = sorted(p.name for p in archive.rglob("*") if p.is_file())
    assert copied == ["1789424859.run", "1789508732.run", "1789515832.run"], copied

    # The empty modded profile must render, not crash.
    empty = sts2data.load_progress(profiles[1]["saves"])
    assert sts2data.character_table(empty).empty
    assert sts2data.card_table(empty).empty
    assert sts2data.totals(empty)["win_rate"] is None

    runs = sts2data.runs_table(archive / profile["label"].replace("/", "_"))
    assert list(runs["character"]) == ["DEFECT", "SILENT", "IRONCLAD"], "newest first"
    assert list(runs["result"]) == ["Abandoned", "Loss", "Win"]
    assert list(runs["ascension"]) == [1, 0, 4]
    assert list(runs["floors_climbed"]) == [2, 6, 5]
    assert list(runs["build"]) == ["v0.107.2", "v0.107.1", "v0.107.1"]
    assert list(runs["seed"]) == ["QLDTDQLGQY", "0VD3JRH6FY", "3J6ZXDRGZE"]
    assert list(runs["run_id"]) == [1789515832, 1789508732, 1789424859], "run_id links the detail page"
    assert runs.loc[1, "killed_by"] == "Owl Magistrate Normal"
    assert runs.loc[0, "killed_by"] == "" and runs.loc[2, "killed_by"] == "", \
        "abandoned and win must not show a killer"
    assert runs.loc[0, "date"] == datetime.fromtimestamp(1789515832).strftime("%Y-%m-%d %H:%M")

    print("loader          ok")


def check_run_detail(archive: Path) -> None:
    archive_dir = archive / f"steam_{USER}_vanilla_profile1"

    assert sts2data.load_run(archive_dir, 1789424859) is not None
    assert sts2data.load_run(archive_dir, 9999999999) is None, "missing run must be None, not raise"

    run = sts2data.load_run(archive_dir, 1789424859)
    summary = sts2data.run_summary(run)
    assert summary["character"] == "IRONCLAD"
    assert summary["result"] == "Win"
    assert summary["seed"] == "3J6ZXDRGZE"
    assert summary["run_time"] == "1:02:05", "run_time is seconds"
    assert summary["floors_climbed"] == 5
    assert summary["deck_size"] == 4 and summary["relic_count"] == 3
    assert summary["final_hp"] == "61/80", "HP from the last floor reached"
    assert summary["badges"] == "Elite (bronze)"
    assert summary["killed_by"] == "", "a win has no killer"

    path = sts2data.run_path_table(run)
    assert list(path["floor"]) == [1, 2, 3, 4, 5], "floor is cumulative across acts"
    assert list(path["act"]) == ["Overgrowth", "Overgrowth", "Hive", "Hive", "Hive"], \
        "acts indexed by position, not zipped"
    assert list(path["type"]) == ["Monster", "Rest Site", "Unknown", "Shop", "Ancient"]
    assert path.loc[0, "monsters"] == "Nibbit \u00d72", "repeated monster ids collapse to a count"
    assert path.loc[1, "room"] == "", "rest sites carry no model_id"
    assert path.loc[2, "room"] == "Dense Vegetation \u2192 Dense Vegetation Event Encounter", \
        "an event leading into a fight records two rooms on one map point"
    assert path.loc[2, "turns"] == 8 and path.loc[2, "monsters"] == "Wriggler \u00d74"
    assert path.loc[0, "hp"] == "78/80" and path.loc[0, "gold"] == 119

    def happened(floor_index: int) -> list[tuple[str, str]]:
        return [(p["text"], p["kind"]) for p in path.loc[floor_index, "happened"]]

    # A picked card is always repeated in cards_gained; it must not double-report.
    assert happened(0) == [
        ("+Setup Strike", "gain"),
        ("skipped Tremble, Blood Wall", "skip"),
    ]
    assert happened(1) == [
        ("Hatch", "note"),
        ("Byrdonis Egg \u2192 Byrd Swoop", "note"),
        ("+Byrdpip", "gain"),
    ], "a rest site can grant a relic; the transform explains where it came from"
    assert happened(2) == [("Rest", "note")], "event option parsed from the .options. key"
    # A shop purchase also appears in relic_choices as picked; only 'bought' should show.
    assert happened(3) == [("bought Miniature Tent", "gain")]
    # The ancient's chosen relic must not also appear as its own note, and the
    # two upgraded copies of Defend collapse to one entry.
    assert happened(4) == [
        ("upgraded Defend Ironclad \u00d72", "note"),
        ("+Yummy Cookie", "gain"),
        ("skipped Storybook", "skip"),
    ]

    deck = sts2data.deck_table(run)
    assert dict(zip(deck["card"], deck["count"])) == {
        "Strike Ironclad": 2, "Bash+": 1, "Stomp (Instinct)": 1,
    }, "duplicates grouped, upgrades and enchantments marked"

    relics = sts2data.relic_table(run)
    assert list(relics["relic"]) == ["Burning Blood", "Byrdpip", "Vajra"], "ordered by floor acquired"
    assert list(relics["source"]) == ["Starting", "Rest Site", "Starting"], \
        "source comes from what the floor recorded, not the room type alone"

    print("run detail      ok")


def table_html(body: str) -> str:
    """The rendered <table>, isolated from the filter <select> options that
    otherwise pollute a plain substring search of the whole page."""
    match = re.search(r"<table.*?</table>", body, re.S)
    return match.group(0) if match else ""


def check_auth() -> None:
    app, client = make_client(username=None)

    # registration
    r = client.post("/register", data={"username": "Tim", "password": PASSWORD})
    assert r.status_code == 302 and r.headers["Location"] == "/u/tim", \
        "registering logs you in and lands on your overview"
    with app.app_context():
        user = User.by_username("tim")
        assert user is not None and user.email is None
        assert PASSWORD not in user.password_hash, "password must never be stored in the clear"
        assert user.password_hash.startswith("scrypt:"), user.password_hash[:20]
        assert user.check_password(PASSWORD) and not user.check_password("wrong")

    assert client.get("/u/tim").status_code == 200
    assert b"tim" in client.get("/").data or client.get("/").status_code == 302

    # logout, then the same account can log back in
    assert client.post("/logout").status_code == 302
    r = client.post("/login", data={"username": "TIM", "password": PASSWORD})
    assert r.status_code == 302 and r.headers["Location"] == "/u/tim", \
        "usernames are case-insensitive"

    # a wrong password and an unknown account must be indistinguishable
    _app2, fresh = make_client(username=None)
    fresh.post("/register", data={"username": "tim", "password": PASSWORD})
    fresh.post("/logout")
    wrong = fresh.post("/login", data={"username": "tim", "password": "nope"})
    missing = fresh.post("/login", data={"username": "ghost", "password": "nope"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.get_data() == missing.get_data(), \
        "a wrong password and an unknown account must be byte-identical"
    assert "Invalid username or password." in wrong.get_data(as_text=True)

    # validation
    _app3, v = make_client(username=None)
    for bad, reason in (
        ({"username": "ab", "password": PASSWORD}, "too short"),
        ({"username": "has space", "password": PASSWORD}, "illegal character"),
        ({"username": "ok-name", "password": "short"}, "weak password"),
    ):
        r = v.post("/register", data=bad)
        assert r.status_code == 400, f"{reason} must be rejected"
    with app.app_context():
        pass
    assert v.post("/register", data={"username": "ok-name", "password": PASSWORD}).status_code == 302
    dupe = v.post("/register", data={"username": "OK-NAME", "password": PASSWORD})
    assert dupe.status_code == 400, "duplicate username must be rejected regardless of case"

    print("auth            ok")


def check_privacy() -> None:
    app, client = make_client("tim")
    with app.app_context():
        other = User(username="someone-else")
        other.set_password(PASSWORD)
        db.session.add(other)
        db.session.commit()

    # someone else's overview is indistinguishable from a name nobody has taken
    assert client.get("/u/someone-else").status_code == 404
    assert client.get("/u/nobody-at-all").status_code == 404
    assert client.get("/u/tim").status_code == 200

    # logged out, protected pages redirect to the landing page rather than render
    _app, anon = make_client(username=None)
    for path in ("/u/tim", "/local", "/local/runs", "/local/run/1789424859"):
        r = anon.get(path)
        assert r.status_code == 302 and r.headers["Location"].startswith("/?next="), \
            f"{path} must require a login, got {r.status_code}"

    print("privacy         ok")


def check_csrf() -> None:
    """CSRF is disabled in TestConfig, so this checks it with protection on."""

    class CsrfConfig(TestConfig):
        WTF_CSRF_ENABLED = True

    app = dashboard.create_app(CsrfConfig)
    client = app.test_client()
    r = client.post("/register", data={"username": "tim", "password": PASSWORD})
    assert r.status_code == 400, "a form without a CSRF token must be rejected"
    assert b"CSRF" in r.data or b"csrf" in r.data

    # the real form carries a token, so a normal browser flow still works
    token = re.search(r'name="csrf_token" value="([^"]+)"',
                      client.get("/").get_data(as_text=True)).group(1)
    r = client.post("/register",
                    data={"username": "tim", "password": PASSWORD, "csrf_token": token})
    assert r.status_code == 302, "a form with a valid token must be accepted"

    print("csrf            ok")


def check_secret_key() -> None:
    """The app must not start on a missing secret key outside debug."""
    import config

    original_key, original_env = config.Config.SECRET_KEY, os.environ.get("FLASK_DEBUG")
    config.Config.SECRET_KEY = None
    os.environ.pop("FLASK_DEBUG", None)
    try:
        try:
            config.resolve()
        except RuntimeError as exc:
            assert "SECRET_KEY" in str(exc)
        else:
            raise AssertionError("a missing SECRET_KEY must refuse to start")

        os.environ["FLASK_DEBUG"] = "1"
        assert config.resolve().SECRET_KEY, "debug mode falls back to a throwaway key"
    finally:
        config.Config.SECRET_KEY = original_key
        os.environ.pop("FLASK_DEBUG", None)
        if original_env is not None:
            os.environ["FLASK_DEBUG"] = original_env

    print("secret key      ok")


def check_upload_form() -> None:
    """The picker must choose a folder, not descend into one."""
    _app, client = make_client("tim")
    form = client.get("/upload").get_data(as_text=True)

    inputs = re.findall(r"<input[^>]*type=\"file\"[^>]*>", form)
    assert len(inputs) == 1, f"expected a single file input, found {len(inputs)}"
    assert "webkitdirectory" in inputs[0], \
        "without webkitdirectory the picker can only descend into folders"
    assert "multiple" in inputs[0]
    assert 'name="files"' in inputs[0]
    assert 'name="modded"' in form, \
        "the checkbox is the only way to tag a saves folder selected inside modded/"

    print("upload form     ok")


def check_upload_folder() -> None:
    """Selecting the whole saves folder must work and must be quiet about it."""
    app, client = make_client("tim")

    run = RUN_FILES["1789424859.run"]
    base = "saves"
    # An in-progress run really does look like a finished one: it carries
    # start_time, players and map_point_history, and has no win field. Left
    # unfiltered it would be stored as a completed loss.
    current_run = {k: v for k, v in run.items() if k != "win"}
    current_run["start_time"] = 1789999999

    folder = [
        (f"{base}/progress.save", json.dumps(VANILLA_PROGRESS).encode()),
        (f"{base}/current_run.save", json.dumps(current_run).encode()),
        (f"{base}/current_run_mp.save", json.dumps(current_run).encode()),
        (f"{base}/prefs.save", b'{"some":"pref"}'),
        (f"{base}/settings.save", b'{"some":"setting"}'),
        (f"{base}/profile.save", b'{"some":"profile"}'),
        (f"{base}/history/1789424859.run", json.dumps(run).encode()),
        (f"{base}/history/1789424859.run.backup", json.dumps(run).encode()),
        (f"{base}/history/1789111111.corrupt", b"garbage"),
    ]
    body = client.post(
        "/upload",
        data={"files": [(io.BytesIO(b), n) for n, b in folder]},
        content_type="multipart/form-data",
    ).get_data(as_text=True)

    with app.app_context():
        runs = list(db.session.scalars(db.select(Run)))
        assert len(runs) == 1, [r.start_time for r in runs]
        assert runs[0].start_time == 1789424859, "only the finished run is stored"
        assert db.session.scalar(
            db.select(db.func.count()).select_from(ProgressSnapshot)
        ) == 1
        assert not db.session.scalar(
            db.select(Run).filter_by(start_time=1789999999)
        ), "current_run.save is an unfinished run and must never be stored"

    summary = re.search(r'<p class="totals">(.*?)</p>', body, re.S).group(1)
    summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", summary)).strip()
    # 9 files in: one finished run, one progress.save, and seven to ignore.
    assert "1 added" in summary, summary
    assert "1 progress file" in summary, summary
    assert "7 other files ignored" in summary, summary
    assert "0 rejected" in summary, "a normal save folder must not report errors"

    # classification is by name, so the noise never gets opened at all
    assert uploads.classify("history/1789424859.run") == uploads.RUN_KIND
    assert uploads.classify("saves/progress.save") == uploads.PROGRESS_KIND
    assert uploads.classify("saves/current_run.save") is None
    assert uploads.classify("saves/current_run_mp.save") is None
    assert uploads.classify("saves/prefs.save") is None
    assert uploads.classify("saves/settings.save") is None
    assert uploads.classify("saves/profile.save") is None
    assert uploads.classify("history/1789424859.run.backup") is None
    assert uploads.classify("history/1789111111.corrupt") is None

    print("upload folder   ok")


def check_upload() -> None:
    app, client = make_client("tim")

    def send(files, modded=False):
        data = {"files": [(io.BytesIO(body), name) for name, body in files]}
        if modded:
            data["modded"] = "1"
        return client.post("/upload", data=data, content_type="multipart/form-data")

    def as_json(obj) -> bytes:
        return json.dumps(obj).encode()

    run = RUN_FILES["1789424859.run"]

    # a good run is stored, with its metadata pulled out into columns
    body = send([("1789424859.run", as_json(run))]).get_data(as_text=True)
    assert "1 added" in re.sub(r"\s+", " ", body).replace("<strong>", "").replace("</strong>", "")
    with app.app_context():
        stored = db.session.scalar(db.select(Run))
        assert stored.start_time == 1789424859
        assert stored.character == "IRONCLAD" and stored.result == "Win"
        assert stored.ascension == 4 and stored.seed == "3J6ZXDRGZE"
        assert stored.build == "v0.107.1" and stored.floors == 5
        assert stored.is_modded is False
        assert stored.data["seed"] == "3J6ZXDRGZE", "the raw file is kept intact"

    # re-uploading the same run changes nothing
    send([("1789424859.run", as_json(run))])
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Run)) == 1

    # the same run twice inside one request must not trip the unique constraint
    send([("a.run", as_json(run)), ("b.run", as_json(run))])
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Run)) == 1

    # rejections
    for name, payload, expected in (
        ("bad.run", b"{not json", "Not valid JSON"),
        ("empty.run", b"   ", "File is empty"),
        ("list.run", b"[1,2,3]", "expected a JSON object"),
        ("nostart.run", as_json({"players": [{}], "map_point_history": []}), "start_time"),
        ("noplayers.run", as_json({"start_time": 1789424859, "map_point_history": []}), "players"),
        ("badhistory.run", as_json({"start_time": 1789424859, "players": [{}],
                                    "map_point_history": "nope"}), "map_point_history"),
        ("future.run", as_json({"start_time": 99999999999, "players": [{}],
                                "map_point_history": []}), "plausible range"),
        ("binary.run", b"\xff\xfe\x00\x01", "Not UTF-8"),
    ):
        body = send([(name, payload)]).get_data(as_text=True)
        assert expected in body, f"{name} should report {expected!r}, got: {body[-400:]}"
    with app.app_context():
        assert db.session.scalar(db.select(db.func.count()).select_from(Run)) == 1, \
            "no rejected file may reach the database"

    # oversized
    huge = as_json({"start_time": 1789424859, "players": [{}], "map_point_history": [],
                    "pad": "x" * (uploads.MAX_RUN_BYTES)})
    assert "limit" in send([("huge.run", huge)]).get_data(as_text=True)

    # progress.save is stored separately, latest wins, and kept per save tree
    body = send([("progress.save", as_json(VANILLA_PROGRESS))]).get_data(as_text=True)
    assert "lifetime totals (vanilla)" in body
    send([("progress.save", as_json(VANILLA_PROGRESS))])
    with app.app_context():
        assert db.session.scalar(
            db.select(db.func.count()).select_from(ProgressSnapshot)
        ) == 1, "re-uploading progress.save replaces it rather than piling up"

    print("upload          ok")


def check_upload_modded() -> None:
    app, client = make_client("tim")

    def send(files, modded=False):
        data = {"files": [(io.BytesIO(json.dumps(o).encode()), n) for n, o in files]}
        if modded:
            data["modded"] = "1"
        return client.post("/upload", data=data, content_type="multipart/form-data")

    run = RUN_FILES["1789424859.run"]
    other = dict(run, start_time=1789424860)
    third = dict(run, start_time=1789424861)

    # detected from the folder path a browser reports for a directory upload
    send([("steam/765/modded/profile1/saves/history/1789424859.run", run)])
    # the checkbox covers selecting individual files, where there is no path
    send([("1789424860.run", other)], modded=True)
    # a vanilla path stays vanilla
    send([("steam/765/profile1/saves/history/1789424861.run", third)])

    with app.app_context():
        by_start = {r.start_time: r.is_modded for r in db.session.scalars(db.select(Run))}
        assert by_start == {1789424859: True, 1789424860: True, 1789424861: False}, by_start

    assert uploads.is_modded_path("a/modded/profile1/x.run") is True
    assert uploads.is_modded_path("a/profile1/x.run") is False
    assert uploads.is_modded_path("modded.run") is False, \
        "a file merely named modded is not in the modded tree"

    # the filename is only ever used for display and never to touch the disk
    assert uploads.display_name("../../etc/passwd") == "passwd"
    assert uploads.display_name("a/b/c/run.run") == "run.run"

    print("upload modded   ok")


def check_upload_privacy() -> None:
    app, client = make_client("tim")
    client.post(
        "/upload",
        data={"files": [(io.BytesIO(json.dumps(RUN_FILES["1789424859.run"]).encode()),
                         "1789424859.run")]},
        content_type="multipart/form-data",
    )

    # a second account must not see the first account's runs
    _app2, other = make_client(None)
    other.post("/register", data={"username": "someone-else", "password": PASSWORD})
    with app.app_context():
        pass
    assert other.get("/u/tim").status_code == 404

    _app3, anon = make_client(None)
    r = anon.get("/upload")
    assert r.status_code == 302 and r.headers["Location"].startswith("/?next="), \
        "uploading must require a login"
    r = anon.post("/upload", data={})
    assert r.status_code == 302, "posting an upload must require a login too"

    print("upload privacy  ok")


def check_routes() -> None:
    _app, client = make_client()
    modded = sts2data.find_profiles()[1]["saves"]

    body = client.get("/local").get_data(as_text=True)
    assert "IRONCLAD" in body and "OFFERING" in body
    assert "LUCKY_ONCE" not in body
    assert "20.0%</strong> win rate" in body
    assert "12 were pruned" in body
    assert 'href="/static/style.css"' in body, "page must link the stylesheet"
    assert client.get("/static/style.css").status_code == 200, "stylesheet must actually be served"

    assert "LUCKY_ONCE" in client.get("/local?min_runs=0").get_data(as_text=True)
    assert "modded save tree" in client.get(f"/local?saves={modded}").get_data(as_text=True)
    assert "IRONCLAD" in client.get("/local?saves=/etc/passwd").get_data(as_text=True), \
        "unknown path must fall back, not read an arbitrary file"
    assert client.get("/local/api/progress").get_json()["character_stats"][0]["id"] == "CHARACTER.IRONCLAD"

    body = client.get("/local/runs").get_data(as_text=True)
    assert "IRONCLAD" in body and "SILENT" in body and "DEFECT" in body
    assert "Owl Magistrate Normal" in body
    assert "3 runs" in body

    body = client.get("/local/runs?character=SILENT").get_data(as_text=True)
    table = table_html(body)
    assert "Owl Magistrate Normal" in table
    assert "IRONCLAD" not in table and "DEFECT" not in table
    assert "1 run" in body and "1 runs" not in body, "singular count"

    body = client.get("/local/runs?result=Abandoned").get_data(as_text=True)
    table = table_html(body)
    assert "DEFECT" in table
    assert "IRONCLAD" not in table and "SILENT" not in table

    body = client.get(f"/local/runs?saves={modded}").get_data(as_text=True)
    assert "0 runs" in body, "empty modded profile must render, not crash"

    body = client.get("/local/runs").get_data(as_text=True)
    assert "3J6ZXDRGZE" in body, "seed column"
    assert '<td class="win">Win</td>' in body and '<td class="loss">Loss</td>' in body
    assert '<td class="abandoned">Abandoned</td>' in body
    assert "/local/run/1789424859" in body, "date cell links to the detail page"

    response = client.get("/local/run/1789424859")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "IRONCLAD" in body and "3J6ZXDRGZE" in body
    assert "Dense Vegetation \u2192 Dense Vegetation Event Encounter" in body
    assert "Strike Ironclad" in body and "Burning Blood" in body
    assert '<span class="gain">+Setup Strike</span>' in body
    assert '<span class="skip">skipped Tremble, Blood Wall</span>' in body, \
        "declined rewards render muted"
    assert "Byrdonis Egg \u2192 Byrd Swoop" in body and "Rest Site" in body, \
        "relic provenance is visible without cross-referencing the path"

    missing = client.get("/local/run/9999999999")
    assert missing.status_code == 404, "unarchived run must 404"
    assert "not in the archive" in missing.get_data(as_text=True)

    print("routes          ok")


def check_missing_saves() -> None:
    original = sts2data.BASE
    sts2data.BASE = Path(tempfile.gettempdir()) / "sts2-definitely-not-here"
    try:
        _app, client = make_client()
        assert "No save data found" in client.get("/local").get_data(as_text=True)
        assert "No save data found" in client.get("/local/runs").get_data(as_text=True)
    finally:
        sts2data.BASE = original
    print("no save data    ok")


def main() -> None:
    original_base, original_archive = sts2data.BASE, sts2data.ARCHIVE
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        build_fixture(root)
        archive = root / "archive"
        sts2data.BASE, sts2data.ARCHIVE = root, archive
        try:
            check_loader(archive)
            check_run_detail(archive)
            check_auth()
            check_privacy()
            check_csrf()
            check_secret_key()
            check_upload()
            check_upload_form()
            check_upload_folder()
            check_upload_modded()
            check_upload_privacy()
            check_routes()
            check_missing_saves()
        finally:
            sts2data.BASE, sts2data.ARCHIVE = original_base, original_archive
            shutil.rmtree(archive, ignore_errors=True)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
