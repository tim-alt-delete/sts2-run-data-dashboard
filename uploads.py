"""Parsing and validation of uploaded save files.

Everything here treats its input as hostile. Files arrive from a browser, so
nothing about their size, encoding, structure or contents can be assumed. This
module only inspects and reports; persisting is the caller's job.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import sts2data

# Real run files are 12-83 KB, and progress.save is larger but still modest.
# These caps are generous against observed data while keeping a single file
# from exhausting memory.
MAX_RUN_BYTES = 2 * 1024 * 1024
MAX_PROGRESS_BYTES = 5 * 1024 * 1024

# The game keeps at most 100 run files, so a whole save folder fits with room
# to spare. Anything beyond this is not a save folder.
MAX_FILES = 500

RUN_KIND = "run"
PROGRESS_KIND = "progress"

# A save folder holds plenty of files that are not run history. current_run.save
# matters most: it is an in-progress run, and it carries start_time, players and
# map_point_history, so it would otherwise validate as a finished run and be
# recorded as a loss. Selecting a whole folder must quietly ignore all of these.
IGNORED_NAMES = {
    "current_run.save",
    "current_run_mp.save",
    "prefs.save",
    "profile.save",
    "settings.save",
}

# Plausible bounds for a run's start_time, in Unix seconds. Slay the Spire 2
# did not exist before 2024, and a timestamp far in the future is a corrupt or
# hand-edited file rather than a real run.
MIN_START_TIME = 1_700_000_000  # late 2023
MAX_START_TIME = 4_102_444_800  # year 2100


@dataclass
class ParsedFile:
    """One uploaded file, after inspection."""

    filename: str
    kind: str | None = None
    error: str | None = None
    skipped: str | None = None
    is_modded: bool = False
    data: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.skipped is None


def display_name(filename: str) -> str:
    """The basename, for showing in a result table.

    Folder uploads put a relative path here. It is never used to touch the
    filesystem, and Jinja escapes it on the way out.
    """
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name[:120] or "(unnamed)"


def is_modded_path(filename: str) -> bool:
    """Whether the browser-reported path sits under the game's modded tree.

    Selecting individual files gives a bare filename with no path, which is why
    the upload form also offers a checkbox.
    """
    parts = (filename or "").replace("\\", "/").lower().split("/")
    return "modded" in parts[:-1]


def classify(filename: str) -> str | None:
    """Which kind of save file this is, by name, or None to ignore it.

    Uploading a whole folder sends everything in it, so this decides what is
    worth opening at all. Name alone is enough here and is deliberately strict:
    contents are still validated afterwards.
    """
    name = display_name(filename).lower()
    if name in IGNORED_NAMES:
        return None
    if name.endswith(".run"):
        return RUN_KIND
    if name == "progress.save":
        return PROGRESS_KIND
    return None


def looks_like_progress(filename: str, obj: dict) -> bool:
    return filename.lower().endswith("progress.save") or (
        "character_stats" in obj or "card_stats" in obj
    )


def validate_run(obj: Any) -> str | None:
    """Why this is not a usable run file, or None if it is."""
    if not isinstance(obj, dict):
        return "Not a run file: expected a JSON object."

    start_time = obj.get("start_time")
    if not isinstance(start_time, int) or isinstance(start_time, bool):
        return "Not a run file: start_time is missing or not a number."
    if not MIN_START_TIME <= start_time <= MAX_START_TIME:
        return f"start_time {start_time} is outside the plausible range."

    players = obj.get("players")
    if not isinstance(players, list) or not players:
        return "Not a run file: players is missing or empty."
    if not all(isinstance(p, dict) for p in players):
        return "Not a run file: players contains something that is not an object."

    history = obj.get("map_point_history")
    if not isinstance(history, list):
        return "Not a run file: map_point_history is missing."
    if not all(isinstance(act, list) for act in history):
        return "Not a run file: map_point_history is not a list of acts."

    if not isinstance(obj.get("win", False), bool):
        return "Not a run file: win is not a boolean."
    if not isinstance(obj.get("was_abandoned", False), bool):
        return "Not a run file: was_abandoned is not a boolean."

    return None


def validate_progress(obj: Any) -> str | None:
    """Why this is not a usable progress.save, or None if it is."""
    if not isinstance(obj, dict):
        return "Not a progress.save: expected a JSON object."
    for key in ("character_stats", "card_stats"):
        value = obj.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
            return f"Not a progress.save: {key} is not a list of objects."
    if "character_stats" not in obj and "card_stats" not in obj:
        return "Not a progress.save: no character_stats or card_stats."
    return None


def unsafe_key(obj: Any) -> str | None:
    """The first field name MongoDB cannot store comfortably, or None.

    A key containing a dot, or starting with a dollar, collides with MongoDB's
    query syntax and is painful to read back even where the server accepts it.
    Nothing the game exports today uses one -- 115 distinct keys across the
    archived runs, none of them affected -- but storing the export verbatim is
    the whole point of this design, and a game update is exactly what would
    introduce one. Catching it here turns a driver error deep inside a write
    into an actionable line on the upload page.

    Iterative rather than recursive: the input is attacker-supplied and deep
    nesting should not cost a stack frame per level.
    """
    stack: list[Any] = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if "." in key or key.startswith("$"):
                    return key
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return None


def run_metadata(obj: dict) -> dict[str, Any]:
    """The columns worth having outside the JSON blob."""
    player = (obj.get("players") or [{}])[0]
    return {
        "start_time": obj["start_time"],
        "character": sts2data.entry(player.get("character")),
        "result": sts2data.run_result(obj),
        "killed_by": sts2data.run_killed_by(obj)[:64],
        "ascension": obj.get("ascension") if isinstance(obj.get("ascension"), int) else 0,
        "seed": str(obj.get("seed") or "")[:32],
        "build": str(obj.get("build_id") or "")[:32],
        "floors": sum(len(act) for act in obj.get("map_point_history", [])),
        "run_time": obj.get("run_time") if isinstance(obj.get("run_time"), int) else 0,
    }


def parse_file(storage, force_modded: bool = False) -> ParsedFile:
    """Inspect one uploaded file without trusting any part of it."""
    filename = storage.filename or ""
    parsed = ParsedFile(
        filename=display_name(filename),
        is_modded=bool(force_modded) or is_modded_path(filename),
    )

    parsed.kind = classify(filename)
    if parsed.kind is None:
        # Not run history. Folder uploads are full of these, so it is not an
        # error, just nothing to do.
        parsed.skipped = "not a run or progress file"
        return parsed

    cap = MAX_PROGRESS_BYTES if parsed.kind == PROGRESS_KIND else MAX_RUN_BYTES
    raw = storage.read(cap + 1)
    if len(raw) > cap:
        parsed.error = f"Larger than the {cap // (1024 * 1024)} MB limit."
        return parsed
    if not raw.strip():
        parsed.error = "File is empty."
        return parsed

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        parsed.error = "Not UTF-8 text, so not a save file."
        return parsed

    try:
        obj = json.loads(text)
    except (ValueError, RecursionError):
        # RecursionError covers pathologically nested JSON built to blow the
        # stack rather than the size limit.
        parsed.error = "Not valid JSON."
        return parsed

    if parsed.kind == PROGRESS_KIND:
        parsed.error = validate_progress(obj)
    else:
        parsed.error = validate_run(obj)

    if parsed.ok:
        bad = unsafe_key(obj)
        if bad is not None:
            parsed.error = (
                f"Field name {bad!r} cannot be stored: names must not contain "
                f"'.' or start with '$'."
            )
            return parsed
        parsed.data = obj
        if parsed.kind == RUN_KIND:
            parsed.metadata = run_metadata(obj)

    return parsed
