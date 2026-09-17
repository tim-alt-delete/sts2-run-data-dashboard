"""Self-check for the dashboard.

Runs without the game installed, against fixture save data and an in-memory
database, so you can confirm the install works before uploading anything real.

    python test_dashboard.py

Fixture key names are taken from the decompiled game source:
SerializableProgress.cs, CharacterStats.cs, CardStats.cs, UserDataPathProvider.cs
"""

import io
import json
import os
import re
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


def check_parsing() -> None:
    run = RUN_FILES["1789424859.run"]
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

    print("parsing         ok")


def check_lifetime_totals() -> None:
    """progress.save now only supplies lifetime figures and the pruning gap."""
    progress = VANILLA_PROGRESS

    assert sts2data.lifetime_totals(progress) == {
        "runs": 15, "wins": 3, "losses": 12, "win_rate": 20.0,
        "playtime": "12:34:56", "floors_climbed": 611,
    }

    assert sts2data.data_loss_report(progress, uploaded=3) == {
        "recorded": 15, "uploaded": 3, "missing": 12,
    }
    assert sts2data.data_loss_report(progress, uploaded=99)["missing"] == 0, \
        "more uploads than the game recorded is not negative loss"

    print("lifetime totals ok")


def make_run(start_time, character, *, win=False, abandoned=False, ascension=0,
             run_time=1000, floors=2, deck=(), choices=()):
    """A run file with only what the aggregates read."""
    return {
        "start_time": start_time, "win": win, "was_abandoned": abandoned,
        "ascension": ascension, "run_time": run_time, "seed": "SEED",
        "build_id": "v0.107.1", "killed_by_encounter": "NONE.NONE",
        "killed_by_event": "NONE.NONE", "acts": ["ACT.OVERGROWTH"],
        "players": [{"character": f"CHARACTER.{character}",
                     "deck": [{"id": c} for c in deck]}],
        "map_point_history": [[
            {"map_point_type": "monster", "rooms": [{"room_type": "monster"}],
             "player_stats": [{"card_choices": [
                 {"card": {"id": cid}, "was_picked": picked}
                 for cid, picked in choices]}]},
        ] + [{"map_point_type": "monster", "rooms": [{"room_type": "monster"}],
              "player_stats": [{}]} for _ in range(floors - 1)]],
    }


def check_derived_aggregates() -> None:
    """Statistics rebuilt from runs, replaying what the game does per run."""
    runs = [
        make_run(1000, "IRONCLAD", win=True, ascension=0, run_time=3600, floors=48),
        make_run(2000, "IRONCLAD", win=True, ascension=1, run_time=1800, floors=48),
        make_run(3000, "IRONCLAD", run_time=600, floors=12),
        make_run(4000, "IRONCLAD", win=True, ascension=2, run_time=2400, floors=48),
        make_run(5000, "SILENT", abandoned=True, run_time=300, floors=3),
    ]

    assert sts2data.totals(runs) == {
        "runs": 5, "wins": 3, "losses": 1, "abandoned": 1,
        "win_rate": 60.0, "playtime": "2:25:00",
        "floors_climbed": 159,
    }

    table = sts2data.character_table(runs).set_index("character")
    iron = table.loc["IRONCLAD"]
    assert (iron["runs"], iron["wins"], iron["losses"], iron["abandoned"]) == (4, 3, 1, 0)
    assert iron["win_rate"] == 75.0
    assert iron["best_streak"] == 2, "two wins, then a loss, then one more"
    assert iron["current_streak"] == 1, "the last run was a win"
    assert iron["max_ascension"] == 3, \
        "each win at the current ascension unlocks the next"
    assert iron["fastest_win"] == "0:30:00", "the quickest win, not the quickest run"
    assert iron["playtime"] == "2:20:00"

    silent = table.loc["SILENT"]
    assert (silent["wins"], silent["losses"], silent["abandoned"]) == (0, 0, 1)
    assert silent["max_ascension"] == 0 and pd.isna(silent["fastest_win"])
    assert silent["current_streak"] == 0, "an abandon ends a streak like a loss"

    # a win only unlocks the next ascension when played at the current maximum
    skipped = [make_run(1000, "DEFECT", win=True, ascension=5)]
    assert sts2data.character_table(skipped).loc[0, "max_ascension"] == 0, \
        "winning above your unlocked level does not advance the ladder"

    assert sts2data.character_table([]).empty
    assert sts2data.totals([])["win_rate"] is None, "no runs must not divide by zero"

    print("derived stats   ok")


def check_card_stats() -> None:
    """Cards count per run they ended in, and per reward screen for pick rate."""
    runs = [
        make_run(1000, "IRONCLAD", win=True,
                 deck=["CARD.STRIKE", "CARD.STRIKE", "CARD.PYRE"],
                 choices=[("CARD.PYRE", True), ("CARD.TREMBLE", False)]),
        make_run(2000, "IRONCLAD",
                 deck=["CARD.STRIKE", "CARD.TREMBLE"],
                 choices=[("CARD.TREMBLE", True), ("CARD.PYRE", False)]),
    ]

    cards = sts2data.card_table(runs, min_runs=0).set_index("card")

    strike = cards.loc["Strike"]
    assert (strike["runs"], strike["wins"], strike["losses"]) == (2, 1, 1), \
        "two copies in one deck is still one run"
    assert strike["win_rate"] == 50.0
    assert pd.isna(strike["pick_rate"]), "a starting card is never offered"

    pyre = cards.loc["Pyre"]
    assert (pyre["wins"], pyre["losses"]) == (1, 0)
    assert (pyre["picked"], pyre["skipped"]) == (1, 1) and pyre["pick_rate"] == 50.0

    tremble = cards.loc["Tremble"]
    assert (tremble["wins"], tremble["losses"]) == (0, 1), \
        "picked in the losing run, skipped in the winning one"

    # sample-size floor
    assert set(sts2data.card_table(runs, min_runs=2)["card"]) == {"Strike"}
    assert sts2data.card_table(runs, min_runs=99).empty
    assert sts2data.card_table([]).empty

    print("card stats      ok")


def check_derived_matches_progress() -> None:
    """The whole premise: runs reproduce progress.save when nothing was pruned.

    This is the manual cross-check that was done by hand against real data,
    pinned down so a change to either side cannot quietly break it.
    """
    runs = [
        make_run(1000, "IRONCLAD", win=True, ascension=0, run_time=3600, floors=48),
        make_run(2000, "IRONCLAD", run_time=1200, floors=30),
        make_run(3000, "SILENT", run_time=900, floors=20),
        make_run(4000, "SILENT", abandoned=True, run_time=300, floors=5),
    ]

    # what the game would have written after exactly these runs
    progress = {
        "total_playtime": 3600 + 1200 + 900 + 300,
        "floors_climbed": 48 + 30 + 20 + 5,
        "character_stats": [
            {"id": "CHARACTER.IRONCLAD", "total_wins": 1, "total_losses": 1,
             "max_ascension": 1, "fastest_win_time": 3600, "best_win_streak": 1,
             "current_streak": 0, "playtime": 4800},
            # the game folds abandons into losses
            {"id": "CHARACTER.SILENT", "total_wins": 0, "total_losses": 2,
             "max_ascension": 0, "fastest_win_time": -1, "best_win_streak": 0,
             "current_streak": 0, "playtime": 1200},
        ],
        "card_stats": [],
    }

    derived = sts2data.totals(runs)
    lifetime = sts2data.lifetime_totals(progress)
    assert derived["runs"] == lifetime["runs"] == 4
    assert derived["wins"] == lifetime["wins"] == 1
    assert derived["losses"] + derived["abandoned"] == lifetime["losses"] == 3, \
        "progress.save counts abandons as losses; runs keep them apart"
    assert derived["playtime"] == lifetime["playtime"]
    assert derived["floors_climbed"] == lifetime["floors_climbed"]

    table = sts2data.character_table(runs).set_index("character")
    for entry in progress["character_stats"]:
        name = entry["id"].split(".")[-1]
        row = table.loc[name]
        assert row["wins"] == entry["total_wins"], name
        assert row["losses"] + row["abandoned"] == entry["total_losses"], name
        assert row["max_ascension"] == entry["max_ascension"], name
        assert row["best_streak"] == entry["best_win_streak"], name
        assert row["current_streak"] == entry["current_streak"], name
        assert row["playtime"] == sts2data.duration(entry["playtime"]), name
        expected = entry["fastest_win_time"]
        if expected < 0:
            assert pd.isna(row["fastest_win"]), name
        else:
            assert row["fastest_win"] == sts2data.duration(expected), name

    assert sts2data.data_loss_report(progress, uploaded=len(runs))["missing"] == 0

    print("derived==progress ok")


def upload_runs(client, names=None, modded=False):
    """Push fixture runs through the real upload route."""
    names = names or list(RUN_FILES)
    data = {"files": [(io.BytesIO(json.dumps(RUN_FILES[n]).encode()), n) for n in names]}
    if modded:
        data["modded"] = "1"
    return client.post("/upload", data=data, content_type="multipart/form-data")


def check_run_pages() -> None:
    _app, client = make_client("tim")
    upload_runs(client)

    body = client.get("/u/tim/runs").get_data(as_text=True)
    assert "3 runs" in body
    assert "IRONCLAD" in body and "SILENT" in body and "DEFECT" in body
    assert "Owl Magistrate Normal" in body, "killed_by comes from its own column now"
    assert "3J6ZXDRGZE" in body, "seed column"
    assert '<td class="win">Win</td>' in body
    assert '<td class="loss">Loss</td>' in body
    assert '<td class="abandoned">Abandoned</td>' in body
    assert "/u/tim/run/1789424859" in body, "date links to the detail page"

    # filters
    table = table_html(client.get("/u/tim/runs?character=SILENT").get_data(as_text=True))
    assert "Owl Magistrate Normal" in table
    assert "IRONCLAD" not in table and "DEFECT" not in table

    body = client.get("/u/tim/runs?result=Abandoned").get_data(as_text=True)
    assert "1 run" in body and "1 runs" not in body, "singular count"
    assert "DEFECT" in table_html(body)

    assert "0 runs" in client.get("/u/tim/runs?character=NOBODY").get_data(as_text=True)

    # detail page, rendered from the stored JSON
    response = client.get("/u/tim/run/1789424859")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "IRONCLAD" in body and "3J6ZXDRGZE" in body
    assert "Dense Vegetation \u2192 Dense Vegetation Event Encounter" in body
    assert '<span class="gain">+Setup Strike</span>' in body
    assert '<span class="skip">skipped Tremble, Blood Wall</span>' in body
    assert "Byrdonis Egg \u2192 Byrd Swoop" in body and "Rest Site" in body

    missing = client.get("/u/tim/run/9999999999")
    assert missing.status_code == 404 and "not in your uploads" in missing.get_data(as_text=True)

    print("run pages       ok")


def check_overview_page() -> None:
    _app, client = make_client("tim")
    upload_runs(client)

    body = client.get("/u/tim").get_data(as_text=True)
    text = text_of(body)
    # the fixtures are one win, one loss and one abandon
    assert "3 runs" in text and "1 wins" in text and "1 losses" in text
    assert "1 abandoned" in text
    assert "33.3% win rate" in text
    assert "IRONCLAD" in body and "SILENT" in body and "DEFECT" in body

    # no progress.save uploaded yet, so the lifetime panel invites one
    assert "lifetime totals" in text

    # card table honours the sample-size floor
    assert "Strike Ironclad" not in body, "a one-run card is below the default floor of 5"
    loose = client.get("/u/tim?min_runs=1").get_data(as_text=True)
    assert "Strike Ironclad" in loose
    assert "Setup Strike" not in loose, \
        "picked but never in a final deck, so it counts for no runs"
    assert "Setup Strike" in client.get("/u/tim?min_runs=0").get_data(as_text=True)

    # uploading progress.save fills in the lifetime panel and the pruning gap
    client.post(
        "/upload",
        data={"files": [(io.BytesIO(json.dumps(VANILLA_PROGRESS).encode()), "progress.save")]},
        content_type="multipart/form-data",
    )
    text = text_of(client.get("/u/tim").get_data(as_text=True))
    assert "reports 15 runs" in text, text[:400]
    assert "12 of those have no run file" in text, "15 recorded, 3 uploaded"

    print("overview page   ok")


def check_run_pages_modded() -> None:
    """Modded runs stay out of the way unless asked for."""
    _app, client = make_client("tim")
    upload_runs(client, ["1789424859.run"])
    upload_runs(client, ["1789508732.run"], modded=True)

    vanilla = client.get("/u/tim/runs").get_data(as_text=True)
    assert "1 run" in vanilla and "IRONCLAD" in vanilla
    assert "SILENT" not in table_html(vanilla), "modded runs are hidden by default"

    modded = client.get("/u/tim/runs?tree=modded").get_data(as_text=True)
    assert "SILENT" in table_html(modded) and "IRONCLAD" not in table_html(modded)

    both = client.get("/u/tim/runs?tree=all").get_data(as_text=True)
    assert "2 runs" in both
    assert "(modded)" in both, "a modded run is labelled when both trees are shown"

    # the overview counts them separately
    overview = client.get("/u/tim").get_data(as_text=True)
    assert "1 modded run kept separate" in text_of(overview)

    print("run pages mod   ok")


def check_run_pages_privacy() -> None:
    _app, client = make_client("tim")
    upload_runs(client, ["1789424859.run"])

    _app2, other = make_client("someone-else")
    # another account cannot reach tim's pages, nor his runs through its own URL
    assert other.get("/u/tim/runs").status_code == 404
    assert other.get("/u/tim/run/1789424859").status_code == 404
    assert other.get("/u/someone-else/run/1789424859").status_code == 404, \
        "runs are scoped to their owner, not global by id"

    _app3, anon = make_client(None)
    for path in ("/u/tim", "/u/tim/runs", "/u/tim/run/1789424859"):
        r = anon.get(path)
        assert r.status_code == 302 and r.headers["Location"].startswith("/?next="), path

    print("run pages priv  ok")


def text_of(html: str) -> str:
    """Visible text with runs of whitespace collapsed.

    Tags have to go before whitespace is collapsed, or removing a tag between
    two words leaves a double space and breaks naive substring checks.
    """
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


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
    for path in ("/u/tim", "/u/tim/runs", "/upload"):
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

    summary = text_of(re.search(r'<p class="totals">(.*?)</p>', body, re.S).group(1))
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


def main() -> None:
    check_parsing()
    check_lifetime_totals()
    check_derived_aggregates()
    check_card_stats()
    check_derived_matches_progress()
    check_auth()
    check_privacy()
    check_csrf()
    check_secret_key()
    check_upload()
    check_upload_form()
    check_upload_folder()
    check_upload_modded()
    check_upload_privacy()
    check_run_pages()
    check_overview_page()
    check_run_pages_modded()
    check_run_pages_privacy()
    print("\nall checks passed")


if __name__ == "__main__":
    main()
