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
from collections import Counter
from datetime import datetime
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

RUN_COLUMNS = [
    "date",
    "character",
    "ascension",
    "result",
    "killed_by",
    "build",
    "floors_climbed",
    "seed",
]

PATH_COLUMNS = [
    "floor",
    "act",
    "type",
    "room",
    "monsters",
    "turns",
    "hp",
    "gold",
    "happened",
]


def entry(model_id: str | None) -> str:
    """'CHARACTER.IRONCLAD' -> 'IRONCLAD'. ModelId serializes as CATEGORY.ENTRY."""
    return (model_id or "?").split(".")[-1]


def humanize(model_id_entry: str) -> str:
    """'OWL_MAGISTRATE_NORMAL' -> 'Owl Magistrate Normal'."""
    return model_id_entry.replace("_", " ").title()


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


def load_runs(archive_dir: Path | str) -> list[dict]:
    """Parse every archived .run file.

    Reads the local archive, not the live history/ dir, so runs stay visible
    after the game prunes them.
    """
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(Path(archive_dir).glob("*.run"))
    ]


def run_result(run: dict) -> str:
    """Win / Loss / Abandoned.

    progress.save lumps abandons in with losses; the run files keep them apart,
    so this is a finer breakdown than the character table can give.
    """
    if run.get("was_abandoned"):
        return "Abandoned"
    return "Win" if run.get("win") else "Loss"


def run_killed_by(run: dict) -> str:
    """What ended the run, humanized. Blank for wins and abandons."""
    killed = run.get("killed_by_encounter") or "NONE.NONE"
    if entry(killed) == "NONE":
        killed = run.get("killed_by_event") or "NONE.NONE"
    return humanize(entry(killed)) if entry(killed) != "NONE" else ""


def runs_table(archive_dir: Path | str) -> pd.DataFrame:
    """One row per archived run, newest first.

    Keeps run_id (the start_time, which is also the filename) so rows can link
    to the detail page.
    """
    rows = []
    for run in load_runs(archive_dir):
        player = (run.get("players") or [{}])[0]
        start_time = run.get("start_time", 0)
        rows.append(
            {
                "run_id": start_time,
                "date": datetime.fromtimestamp(start_time).strftime("%Y-%m-%d %H:%M"),
                "character": entry(player.get("character")),
                "ascension": run.get("ascension", 0),
                "result": run_result(run),
                "killed_by": run_killed_by(run),
                "build": run.get("build_id", "?"),
                "floors_climbed": sum(len(act) for act in run.get("map_point_history", [])),
                "seed": run.get("seed", ""),
            }
        )

    table = pd.DataFrame(rows, columns=["run_id", *RUN_COLUMNS])
    return table.sort_values("run_id", ascending=False, ignore_index=True)


def load_run(archive_dir: Path | str, run_id: int) -> dict | None:
    """One archived run by id. The id is the start_time and the filename."""
    path = Path(archive_dir) / f"{run_id}.run"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def run_summary(run: dict) -> dict:
    """Headline facts for the top of the run detail page."""
    player = (run.get("players") or [{}])[0]
    history = run.get("map_point_history", [])

    final = {}
    for act in history:
        for point in act:
            for stats in point.get("player_stats", []):
                final = stats

    return {
        "run_id": run.get("start_time", 0),
        "date": datetime.fromtimestamp(run.get("start_time", 0)).strftime("%Y-%m-%d %H:%M"),
        "character": entry(player.get("character")),
        "result": run_result(run),
        "ascension": run.get("ascension", 0),
        "game_mode": (run.get("game_mode") or "").title(),
        "seed": run.get("seed", ""),
        "build": run.get("build_id", "?"),
        "run_time": duration(run.get("run_time", 0)),
        "floors_climbed": sum(len(act) for act in history),
        "killed_by": run_killed_by(run),
        "final_hp": f"{final.get('current_hp', 0)}/{final.get('max_hp', 0)}" if final else "",
        "deck_size": len(player.get("deck") or []),
        "relic_count": len(player.get("relics") or []),
        "badges": ", ".join(
            f"{humanize(b.get('id', ''))} ({b.get('rarity', '')})"
            for b in player.get("badges") or []
        ),
    }


def event_choice_text(choice: dict) -> str:
    """The option taken at an event, e.g. 'Take'.

    title.table discriminates two shapes. 'events' keys look like
    NAME.pages.PAGE.options.OPTION.title. 'relics' keys are just
    RELIC_NAME.title and only occur at ancients, where ancient_choice already
    names the relic, so they are skipped to avoid reporting it twice.
    """
    title = choice.get("title") or {}
    key = title.get("key") or ""
    if title.get("table") != "events" or ".options." not in key:
        return ""
    parts = key.split(".")
    return humanize(parts[parts.index("options") + 1])


def floor_events(stats: dict) -> list[dict]:
    """What happened on one floor, as {text, kind} parts.

    kind is 'gain' for things acquired, 'skip' for rewards declined (rendered
    muted), and 'note' for everything else.

    Two sources overlap and must be subtracted, or entries double-report:
    every picked card also appears in cards_gained, and every shop purchase
    also appears in relic_choices/potion_choices as picked.
    """
    parts: list[dict] = []

    def add(text: str, kind: str = "note") -> None:
        if text:
            parts.append({"text": text, "kind": kind})

    def names(counter: Counter) -> list[str]:
        return [
            humanize(entry(i)) if n == 1 else f"{humanize(entry(i))} \u00d7{n}"
            for i, n in counter.items()
        ]

    # What was chosen.
    for choice in stats.get("event_choices") or []:
        add(event_choice_text(choice))
    for choice in stats.get("rest_site_choices") or []:
        add(humanize(choice))

    # Deck edits.
    for change in stats.get("cards_transformed") or []:
        original = describe_card(change.get("original_card") or {})
        final = describe_card(change.get("final_card") or {})
        add(f"{original} \u2192 {final}")
    for change in stats.get("cards_enchanted") or []:
        add(f"enchanted {describe_card(change.get('card') or {})}")
    for card in stats.get("upgraded_cards") or []:
        add(f"upgraded {humanize(entry(card))}")
    for card in stats.get("cards_removed") or []:
        add(f"removed {describe_card(card)}")

    # Purchases, which also show up as picked choices below.
    bought_cards = Counter(stats.get("bought_colorless") or [])
    bought_relics = Counter(stats.get("bought_relics") or [])
    bought_potions = Counter(stats.get("bought_potions") or [])

    # Card rewards: picked cards are always repeated in cards_gained, so
    # subtract them to leave only cards granted without a choice.
    picked_cards: Counter = Counter()
    skipped: list[str] = []
    for choice in stats.get("card_choices") or []:
        card = choice.get("card") or {}
        if choice.get("was_picked"):
            picked_cards[card.get("id")] += 1
        else:
            skipped.append(describe_card(card))

    # An ancient's chosen option is always also a picked relic below, so only
    # the options turned down are worth reporting here.
    for choice in stats.get("ancient_choice") or []:
        if not choice.get("was_chosen"):
            skipped.append(humanize(choice.get("TextKey", "")))

    gained = Counter(c.get("id") for c in stats.get("cards_gained") or [])
    for name in names(picked_cards + (gained - picked_cards - bought_cards)):
        add(f"+{name}", "gain")

    for key, bought in (("relic_choices", bought_relics), ("potion_choices", bought_potions)):
        for choice in stats.get(key) or []:
            name = humanize(entry(choice.get("choice")))
            if not choice.get("was_picked"):
                skipped.append(name)
            elif choice.get("choice") not in bought:
                add(f"+{name}", "gain")

    for name in names(bought_cards + bought_relics + bought_potions):
        add(f"bought {name}", "gain")

    for potion in stats.get("potion_used") or []:
        add(f"used {humanize(entry(potion))}")
    for potion in stats.get("potion_discarded") or []:
        add(f"discarded {humanize(entry(potion))}")

    if skipped:
        add("skipped " + ", ".join(skipped), "skip")

    # Collapse repeats: upgrading two copies of Defend, or gaining two of the
    # same potion, records two identical entries.
    merged: list[dict] = []
    index: dict[tuple[str, str], dict] = {}
    for part in parts:
        key = (part["text"], part["kind"])
        if key in index:
            index[key]["count"] += 1
        else:
            item = {**part, "count": 1}
            index[key] = item
            merged.append(item)

    return [
        {
            "text": i["text"] if i["count"] == 1 else f"{i['text']} \u00d7{i['count']}",
            "kind": i["kind"],
        }
        for i in merged
    ]


def describe_monsters(room: dict) -> str:
    """'Wriggler x4'. Monster lists repeat the same id per copy."""
    counts: dict[str, int] = {}
    for monster in room.get("monster_ids") or []:
        name = humanize(entry(monster))
        counts[name] = counts.get(name, 0) + 1
    return ", ".join(n if c == 1 else f"{n} \u00d7{c}" for n, c in counts.items())


def run_path_table(run: dict) -> pd.DataFrame:
    """Floor by floor. One row per map point.

    A map point can hold more than one room: an event that leads into a fight
    records both, so rooms are joined rather than indexed.
    """
    acts = run.get("acts") or []
    rows = []
    floor = 0

    for act_index, act in enumerate(run.get("map_point_history", [])):
        # acts always lists the full planned run, even if it ended in act 1.
        act_name = humanize(entry(acts[act_index])) if act_index < len(acts) else ""

        for point in act:
            floor += 1
            rooms = point.get("rooms") or []
            stats = (point.get("player_stats") or [{}])[0]

            # Rest sites, treasure and shops carry no model_id, only a room_type.
            names = [humanize(entry(r.get("model_id"))) for r in rooms if r.get("model_id")]
            monsters = [m for m in (describe_monsters(r) for r in rooms) if m]

            rows.append(
                {
                    "floor": floor,
                    "act": act_name,
                    "type": humanize(point.get("map_point_type") or ""),
                    "room": " \u2192 ".join(names),
                    "monsters": ", ".join(monsters),
                    "turns": sum(r.get("turns_taken", 0) for r in rooms),
                    "hp": f"{stats.get('current_hp', 0)}/{stats.get('max_hp', 0)}",
                    "gold": stats.get("current_gold", 0),
                    "happened": floor_events(stats),
                }
            )

    return pd.DataFrame(rows, columns=PATH_COLUMNS)


def describe_card(card: dict) -> str:
    """'Strike', 'Defend+', 'Stomp+ (Instinct)'."""
    name = humanize(entry(card.get("id")))
    name += "+" * card.get("current_upgrade_level", 0)
    enchantment = (card.get("enchantment") or {}).get("id")
    if enchantment:
        name += f" ({humanize(entry(enchantment))})"
    return name


def deck_table(run: dict) -> pd.DataFrame:
    """Final deck, grouped so duplicates read as 'Strike x5'."""
    player = (run.get("players") or [{}])[0]
    counts: dict[str, int] = {}
    for card in player.get("deck") or []:
        name = describe_card(card)
        counts[name] = counts.get(name, 0) + 1

    rows = [{"card": name, "count": count} for name, count in sorted(counts.items())]
    return pd.DataFrame(rows, columns=["card", "count"])


def relic_table(run: dict) -> pd.DataFrame:
    """Relics held at the end, with where each came from.

    The source is worked out from what the floor actually recorded, not from
    the floor's room type alone: a rest site can grant a relic (hatching an
    egg), and a relic that no floor records acquiring is a starting relic.
    """
    acquired: dict[str, str] = {}
    floor = 0
    for act in run.get("map_point_history", []):
        for point in act:
            floor += 1
            stats = (point.get("player_stats") or [{}])[0]
            point_type = humanize(point.get("map_point_type") or "")

            for relic in stats.get("bought_relics") or []:
                acquired[f"{relic}@{floor}"] = "Shop"
            for choice in stats.get("relic_choices") or []:
                if choice.get("was_picked"):
                    acquired.setdefault(f"{choice.get('choice')}@{floor}", point_type)

    player = (run.get("players") or [{}])[0]
    rows = []
    for relic in player.get("relics") or []:
        at = relic.get("floor_added_to_deck", 0)
        rows.append(
            {
                "relic": humanize(entry(relic.get("id"))),
                "floor": at,
                "source": acquired.get(f"{relic.get('id')}@{at}", "Starting"),
            }
        )

    table = pd.DataFrame(rows, columns=["relic", "floor", "source"])
    return table.sort_values("floor", ignore_index=True)


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
