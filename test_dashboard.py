"""Self-check for the dashboard, against a synthetic save tree.

Runs without the game installed. Verifies path discovery, parsing and the
Flask routes, so you can confirm the install works before pointing it at real
save data.

    python test_dashboard.py

Fixture key names are taken from the decompiled game source:
SerializableProgress.cs, CharacterStats.cs, CardStats.cs, UserDataPathProvider.cs
"""

import json
import shutil
import tempfile
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


def build_fixture(root: Path) -> None:
    vanilla = root / "steam" / USER / "profile1" / "saves"
    modded = root / "steam" / USER / "modded" / "profile1" / "saves"
    (vanilla / "history").mkdir(parents=True)
    modded.mkdir(parents=True)

    (vanilla / "progress.save").write_text(json.dumps(VANILLA_PROGRESS), encoding="utf-8")
    (modded / "progress.save").write_text(json.dumps(MODDED_PROGRESS), encoding="utf-8")

    for name in ["1789508732.run", "1789515832.run", "1789424859.run"]:
        (vanilla / "history" / name).write_text('{"win": false}', encoding="utf-8")
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

    print("loader          ok")


def check_routes() -> None:
    client = dashboard.app.test_client()
    modded = sts2data.find_profiles()[1]["saves"]

    body = client.get("/").get_data(as_text=True)
    assert "IRONCLAD" in body and "OFFERING" in body
    assert "LUCKY_ONCE" not in body
    assert "20.0%</strong> win rate" in body
    assert "12 were pruned" in body

    assert "LUCKY_ONCE" in client.get("/?min_runs=0").get_data(as_text=True)
    assert "modded save tree" in client.get(f"/?saves={modded}").get_data(as_text=True)
    assert "IRONCLAD" in client.get("/?saves=/etc/passwd").get_data(as_text=True), \
        "unknown path must fall back, not read an arbitrary file"
    assert client.get("/api/progress").get_json()["character_stats"][0]["id"] == "CHARACTER.IRONCLAD"

    print("routes          ok")


def check_missing_saves() -> None:
    original = sts2data.BASE
    sts2data.BASE = Path(tempfile.gettempdir()) / "sts2-definitely-not-here"
    try:
        assert "No save data found" in dashboard.app.test_client().get("/").get_data(as_text=True)
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
            check_routes()
            check_missing_saves()
        finally:
            sts2data.BASE, sts2data.ARCHIVE = original_base, original_archive
            shutil.rmtree(archive, ignore_errors=True)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
