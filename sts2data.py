"""Read Slay the Spire 2 save data into pandas DataFrames.

The game writes plain UTF-8 JSON, unencrypted, via temp-file + atomic rename,
so these files are safe to read while the game is running.

Layout (macOS):
    ~/Library/Application Support/SlayTheSpire2/{platform}/{userId}/[modded/]profile{N}/saves/
        progress.save        lifetime aggregates
        history/{start}.run  one file per finished run, pruned at 100 files / 5 MB

Loading any mod flips the game to the separate `modded/` tree, which starts as a
fresh default profile. Modded runs never touch vanilla stats.

Run this file directly to check path discovery and parsing:
    python sts2data.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd

BASE = Path.home() / "Library/Application Support/SlayTheSpire2"
ARCHIVE = Path(__file__).resolve().parent / "archive"

CHARACTER_COLUMNS = [
    "character",
    "runs",
    "wins",
    "losses",
    "win_rate",
    "best_streak",
    "current_streak",
    "max_ascension",
    "fastest_win",
    "playtime",
]

CARD_COLUMNS = [
    "card",
    "runs",
    "wins",
    "losses",
    "win_rate",
    "picked",
    "skipped",
    "pick_rate",
]


def entry(model_id: str | None) -> str:
    """'CHARACTER.IRONCLAD' -> 'IRONCLAD'. ModelId serializes as CATEGORY.ENTRY."""
    return (model_id or "?").split(".")[-1]


def percent(part: int, whole: int) -> float | None:
    """Percentage, or None when there is nothing to divide by."""
    if whole <= 0:
        return None
    return round(100 * part / whole, 1)


def duration(seconds: int | None) -> str | None:
    """Seconds to H:MM:SS. The game stores Unix-timestamp deltas, and uses -1
    for 'no win recorded yet'."""
    if seconds is None or seconds < 0:
        return None
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}"


def find_profiles(base: Path | str | None = None) -> list[dict]:
    """Every profile with a progress.save, vanilla and modded.

    The Steam ID is discovered, never hardcoded. Pass `base` to read a copied
    or backed-up save tree instead of the live one.
    """
    base = Path(base) if base else BASE
    saves_dirs = sorted(base.glob("*/*/profile*/saves"))
    saves_dirs += sorted(base.glob("*/*/modded/profile*/saves"))

    profiles = []
    for saves in saves_dirs:
        if not (saves / "progress.save").exists():
            continue

        relative = saves.relative_to(base).parts  # platform/userId/[modded]/profileN/saves
        is_modded = "modded" in relative
        platform, user_id = relative[0], relative[1]

        try:
            runs = total_runs(load_progress(saves))
        except (OSError, ValueError):
            runs = 0

        profiles.append(
            {
                "label": f"{platform}/{user_id}/{'modded' if is_modded else 'vanilla'}/{saves.parent.name}",
                "saves": str(saves),
                "is_modded": is_modded,
                "runs": runs,
                "run_files": len(run_files(saves)),
            }
        )
    return profiles


def default_profile(profiles: list[dict]) -> dict:
    """Prefer vanilla, then whichever holds the most runs.

    Not most-recently-modified: the modded tree is newer but typically empty.
    """
    return max(profiles, key=lambda p: (not p["is_modded"], p["runs"]))


def load_progress(saves_dir: Path | str) -> dict:
    """Parse progress.save."""
    path = Path(saves_dir) / "progress.save"
    return json.loads(path.read_text(encoding="utf-8"))


def run_files(saves_dir: Path | str) -> list[Path]:
    """Finished-run files. The *.run glob excludes the game's own .backup and
    .corrupt siblings."""
    return sorted((Path(saves_dir) / "history").glob("*.run"))


def total_runs(progress: dict) -> int:
    """Lifetime finished runs, summed across characters."""
    return sum(
        c.get("total_wins", 0) + c.get("total_losses", 0)
        for c in progress.get("character_stats", [])
    )


def totals(progress: dict) -> dict:
    """Headline numbers for the top of the dashboard."""
    wins = sum(c.get("total_wins", 0) for c in progress.get("character_stats", []))
    losses = sum(c.get("total_losses", 0) for c in progress.get("character_stats", []))
    return {
        "runs": wins + losses,
        "wins": wins,
        "losses": losses,
        "win_rate": percent(wins, wins + losses),
        "playtime": duration(progress.get("total_playtime", 0)),
        "floors_climbed": progress.get("floors_climbed", 0),
    }


def character_table(progress: dict) -> pd.DataFrame:
    """Win/loss per character, from character_stats."""
    rows = []
    for c in progress.get("character_stats", []):
        wins = c.get("total_wins", 0)
        losses = c.get("total_losses", 0)
        rows.append(
            {
                "character": entry(c.get("id")),
                "runs": wins + losses,
                "wins": wins,
                "losses": losses,
                "win_rate": percent(wins, wins + losses),
                "best_streak": c.get("best_win_streak", 0),
                "current_streak": c.get("current_streak", 0),
                "max_ascension": c.get("max_ascension", 0),
                "fastest_win": duration(c.get("fastest_win_time", -1)),
                "playtime": duration(c.get("playtime", 0)),
            }
        )

    table = pd.DataFrame(rows, columns=CHARACTER_COLUMNS)
    return table.sort_values("runs", ascending=False, ignore_index=True)


def card_table(progress: dict, min_runs: int = 5) -> pd.DataFrame:
    """Win rate per card, from card_stats.

    min_runs floors the sample size. Without it a card seen in a single winning
    run reads 100% and sorts to the top, which makes the table useless.
    """
    rows = []
    for c in progress.get("card_stats", []):
        wins = c.get("times_won", 0)
        losses = c.get("times_lost", 0)
        picked = c.get("times_picked", 0)
        skipped = c.get("times_skipped", 0)
        rows.append(
            {
                "card": entry(c.get("id")),
                "runs": wins + losses,
                "wins": wins,
                "losses": losses,
                "win_rate": percent(wins, wins + losses),
                "picked": picked,
                "skipped": skipped,
                "pick_rate": percent(picked, picked + skipped),
            }
        )

    table = pd.DataFrame(rows, columns=CARD_COLUMNS)
    table = table[table["runs"] >= min_runs]
    return table.sort_values(
        "win_rate", ascending=False, na_position="last", ignore_index=True
    )


def archive_runs(saves_dir: Path | str, label: str, archive: Path | str | None = None) -> int:
    """Copy new .run files into a local archive, return how many were new.

    The game prunes history to 100 files / 5 MB, so runs disappear permanently.
    Called on every dashboard load, which is what keeps the archive current.
    """
    destination = Path(archive) if archive else ARCHIVE
    destination = destination / label.replace("/", "_")
    destination.mkdir(parents=True, exist_ok=True)

    copied = 0
    for source in run_files(saves_dir):
        target = destination / source.name
        if not target.exists():
            shutil.copy2(source, target)
            copied += 1
    return copied


def data_loss_report(progress: dict, saves_dir: Path | str) -> dict:
    """How many finished runs the game no longer has files for.

    progress.save counts every run forever; history/ gets pruned. The gap is
    what has already been lost.
    """
    recorded = total_runs(progress)
    on_disk = len(run_files(saves_dir))
    return {
        "recorded": recorded,
        "on_disk": on_disk,
        "missing": max(0, recorded - on_disk),
    }


def main() -> None:
    profiles = find_profiles()
    if not profiles:
        print(f"No Slay the Spire 2 save data found under {BASE}")
        return

    print(f"Profiles found under {BASE}:")
    for p in profiles:
        print(f"  {p['label']}  runs={p['runs']}  run_files={p['run_files']}")

    profile = default_profile(profiles)
    print(f"\nUsing {profile['label']}")

    progress = load_progress(profile["saves"])
    summary = totals(progress)
    print(
        f"Runs {summary['runs']}  Wins {summary['wins']}  Losses {summary['losses']}"
        f"  Win rate {summary['win_rate']}%  Playtime {summary['playtime']}"
    )

    print("\nPer character:")
    print(character_table(progress).to_string(index=False, na_rep="-"))

    loss = data_loss_report(progress, profile["saves"])
    print(
        f"\nRun files: {loss['on_disk']} on disk, {loss['recorded']} runs recorded, "
        f"{loss['missing']} already pruned by the game"
    )
    print(f"Archived {archive_runs(profile['saves'], profile['label'])} new run files to {ARCHIVE}")


if __name__ == "__main__":
    main()
