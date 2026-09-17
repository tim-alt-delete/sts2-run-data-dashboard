"""Flask dashboard for Slay the Spire 2 stats.

    pip install -r requirements.txt
    flask --app app run --debug
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template, request

import sts2data

app = Flask(__name__)

TABLE_OPTIONS = {"index": False, "na_rep": "-", "classes": "stats", "border": 0}


def selected_profile(profiles: list[dict]) -> dict:
    """Profile named by ?saves=, else the default. Unknown paths fall back
    rather than reading somewhere the user did not ask for."""
    wanted = request.args.get("saves")
    for profile in profiles:
        if profile["saves"] == wanted:
            return profile
    return sts2data.default_profile(profiles)


@app.route("/")
def index():
    profiles = sts2data.find_profiles()
    if not profiles:
        return render_template("index.html", error=f"No save data found under {sts2data.BASE}")

    profile = selected_profile(profiles)
    progress = sts2data.load_progress(profile["saves"])
    min_runs = request.args.get("min_runs", default=5, type=int)

    return render_template(
        "index.html",
        profiles=profiles,
        profile=profile,
        min_runs=min_runs,
        totals=sts2data.totals(progress),
        loss=sts2data.data_loss_report(progress, profile["saves"]),
        archived=sts2data.archive_runs(profile["saves"], profile["label"]),
        characters=sts2data.character_table(progress).to_html(**TABLE_OPTIONS),
        cards=sts2data.card_table(progress, min_runs).to_html(**TABLE_OPTIONS),
    )


@app.route("/api/progress")
def api_progress():
    """Raw progress.save, for poking at from a notebook."""
    profiles = sts2data.find_profiles()
    if not profiles:
        return jsonify(error="No save data found"), 404
    return jsonify(sts2data.load_progress(selected_profile(profiles)["saves"]))


def archive_dir_for(profile: dict):
    """Where this profile's runs are archived."""
    return sts2data.ARCHIVE / profile["label"].replace("/", "_")


@app.route("/runs")
def runs():
    profiles = sts2data.find_profiles()
    if not profiles:
        return render_template("runs.html", error=f"No save data found under {sts2data.BASE}")

    profile = selected_profile(profiles)
    sts2data.archive_runs(profile["saves"], profile["label"])
    table = sts2data.runs_table(archive_dir_for(profile))

    characters = sorted(c for c in table["character"].unique() if c)
    character = request.args.get("character", "")
    result = request.args.get("result", "")

    if character:
        table = table[table["character"] == character]
    if result:
        table = table[table["result"] == result]

    return render_template(
        "runs.html",
        profiles=profiles,
        profile=profile,
        characters=characters,
        character=character,
        result=result,
        run_count=len(table),
        columns=sts2data.RUN_COLUMNS,
        rows=table.to_dict("records"),
    )


@app.route("/run/<int:run_id>")
def run_detail(run_id: int):
    profiles = sts2data.find_profiles()
    if not profiles:
        return render_template("run.html", error=f"No save data found under {sts2data.BASE}"), 404

    profile = selected_profile(profiles)
    run = sts2data.load_run(archive_dir_for(profile), run_id)
    if run is None:
        return render_template(
            "run.html",
            profile=profile,
            error=f"Run {run_id} is not in the archive for {profile['label']}.",
        ), 404

    return render_template(
        "run.html",
        profile=profile,
        summary=sts2data.run_summary(run),
        path_columns=sts2data.PATH_COLUMNS,
        path_rows=sts2data.run_path_table(run).to_dict("records"),
        deck=sts2data.deck_table(run).to_html(**TABLE_OPTIONS),
        relics=sts2data.relic_table(run).to_html(**TABLE_OPTIONS),
    )


if __name__ == "__main__":
    app.run(debug=True)
