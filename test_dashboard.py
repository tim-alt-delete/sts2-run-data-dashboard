"""Self-check for the dashboard, against a synthetic save tree.

Runs without the game installed. Verifies path discovery, parsing and the
Flask routes, so you can confirm the install works before pointing it at real
save data.

    python test_dashboard.py

Fixture key names are taken from the decompiled game source:
SerializableProgress.cs, CharacterStats.cs, CardStats.cs, UserDataPathProvider.cs
"""

import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

import app as dashboard
import sts2data

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


def check_routes() -> None:
    client = dashboard.app.test_client()
    modded = sts2data.find_profiles()[1]["saves"]

    body = client.get("/").get_data(as_text=True)
    assert "IRONCLAD" in body and "OFFERING" in body
    assert "LUCKY_ONCE" not in body
    assert "20.0%</strong> win rate" in body
    assert "12 were pruned" in body
    assert 'href="/static/style.css"' in body, "page must link the stylesheet"
    assert client.get("/static/style.css").status_code == 200, "stylesheet must actually be served"

    assert "LUCKY_ONCE" in client.get("/?min_runs=0").get_data(as_text=True)
    assert "modded save tree" in client.get(f"/?saves={modded}").get_data(as_text=True)
    assert "IRONCLAD" in client.get("/?saves=/etc/passwd").get_data(as_text=True), \
        "unknown path must fall back, not read an arbitrary file"
    assert client.get("/api/progress").get_json()["character_stats"][0]["id"] == "CHARACTER.IRONCLAD"

    body = client.get("/runs").get_data(as_text=True)
    assert "IRONCLAD" in body and "SILENT" in body and "DEFECT" in body
    assert "Owl Magistrate Normal" in body
    assert "3 runs" in body

    body = client.get("/runs?character=SILENT").get_data(as_text=True)
    table = table_html(body)
    assert "Owl Magistrate Normal" in table
    assert "IRONCLAD" not in table and "DEFECT" not in table
    assert "1 run" in body and "1 runs" not in body, "singular count"

    body = client.get("/runs?result=Abandoned").get_data(as_text=True)
    table = table_html(body)
    assert "DEFECT" in table
    assert "IRONCLAD" not in table and "SILENT" not in table

    body = client.get(f"/runs?saves={modded}").get_data(as_text=True)
    assert "0 runs" in body, "empty modded profile must render, not crash"

    body = client.get("/runs").get_data(as_text=True)
    assert "3J6ZXDRGZE" in body, "seed column"
    assert '<td class="win">Win</td>' in body and '<td class="loss">Loss</td>' in body
    assert '<td class="abandoned">Abandoned</td>' in body
    assert "/run/1789424859" in body, "date cell links to the detail page"

    response = client.get("/run/1789424859")
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

    missing = client.get("/run/9999999999")
    assert missing.status_code == 404, "unarchived run must 404"
    assert "not in the archive" in missing.get_data(as_text=True)

    print("routes          ok")


def check_missing_saves() -> None:
    original = sts2data.BASE
    sts2data.BASE = Path(tempfile.gettempdir()) / "sts2-definitely-not-here"
    try:
        client = dashboard.app.test_client()
        assert "No save data found" in client.get("/").get_data(as_text=True)
        assert "No save data found" in client.get("/runs").get_data(as_text=True)
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
            check_routes()
            check_missing_saves()
        finally:
            sts2data.BASE, sts2data.ARCHIVE = original_base, original_archive
            shutil.rmtree(archive, ignore_errors=True)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
