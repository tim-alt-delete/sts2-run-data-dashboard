"""Slay the Spire 2 stats dashboard.

    pip install -r requirements.txt
    docker compose up -d
    export FLASK_DEBUG=1
    flask --app app run --debug

For anything other than local development, set a real secret key:

    python -c 'import secrets; print(secrets.token_hex(32))'
    export SECRET_KEY=...
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect

import config as app_config
import db
import models
import sts2data
import uploads
from models import User, normalize_username, password_error, username_error

TABLE_OPTIONS = {"index": False, "na_rep": "-", "classes": "stats", "border": 0}

csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = "index"
login_manager.login_message = "Log in to see that page."


@login_manager.user_loader
def load_user(user_id: str):
    return User.by_id(user_id)


def create_app(config: type[app_config.Config] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(app_config.resolve(config))

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        # Collections need no creating, so this only declares the uniqueness
        # the app relies on. It is also the first thing to touch the server,
        # which is where an unreachable database gets reported.
        db.ensure_indexes()

    register_auth_routes(app)
    register_user_routes(app)
    return app


def safe_next(target: str | None, fallback: str) -> str:
    """Only ever redirect within this site.

    An attacker-supplied ?next=https://evil.example would otherwise turn the
    login page into an open redirect.
    """
    if not target:
        return fallback
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/"):
        return fallback
    return target


def home_for(user: User) -> str:
    return url_for("overview", username=user.username)


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------


def register_auth_routes(app: Flask) -> None:
    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(home_for(current_user))
        return render_template("index.html", next=request.args.get("next", ""))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return redirect(url_for("index"))

        next_url = request.form.get("next", "")
        username = normalize_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        user = User.by_username(username)

        # Deliberately identical whether the account is missing or the password
        # is wrong, so the form cannot be used to discover who has an account.
        if user is None or not user.check_password(password):
            flash("Invalid username or password.", "error")
            return render_template("index.html", next=next_url), 401

        login_user(user)
        return redirect(safe_next(next_url, home_for(user)))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "GET":
            return redirect(url_for("index"))

        username = normalize_username(request.form.get("username", ""))
        password = request.form.get("password", "")
        email = (request.form.get("email") or "").strip() or None

        def reject(message: str):
            flash(message, "error")
            return render_template("index.html", next=""), 400

        problem = username_error(username) or password_error(password)
        if problem:
            return reject(problem)
        if User.by_username(username) is not None:
            return reject("That username is taken.")
        if email and models.email_taken(email):
            return reject("That email is already registered.")

        user = models.create_user(username, email, password)

        login_user(user)
        return redirect(home_for(user))

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))


# --------------------------------------------------------------------------
# per-user pages
# --------------------------------------------------------------------------


def register_user_routes(app: Flask) -> None:
    def owned(username: str):
        """The signed-in user, or 404.

        Profiles are private. A 403 would confirm the account exists, so
        someone else's page is indistinguishable from a name nobody has taken.
        """
        if normalize_username(username) != current_user.username:
            abort(404)
        return current_user

    @app.route("/u/<username>")
    @login_required
    def overview(username: str):
        user = owned(username)
        tree = request.args.get("tree", "vanilla")
        min_runs = request.args.get("min_runs", default=5, type=int)

        by_tree = models.run_counts_by_tree(user.id)

        # Every statistic is rebuilt from the run files on each request. That
        # means loading each run's JSON, which is fine for hundreds of runs and
        # wasteful for many thousands. Cache or precompute when that day comes.
        data = models.run_data_for_user(user.id, tree)

        # progress.save covers runs the game has since pruned, so its totals can
        # exceed what was uploaded. That gap is the point of showing it.
        snapshot = models.progress_snapshot(user.id, is_modded=(tree == "modded"))
        lifetime = sts2data.lifetime_totals(snapshot["data"]) if snapshot else None
        loss = (
            sts2data.data_loss_report(snapshot["data"], len(data)) if snapshot else None
        )

        return render_template(
            "overview.html",
            user=user,
            tree=tree,
            min_runs=min_runs,
            run_count=by_tree.get(False, 0),
            modded_count=by_tree.get(True, 0),
            totals=sts2data.totals(data),
            lifetime=lifetime,
            loss=loss,
            characters=sts2data.character_table(data).to_html(**TABLE_OPTIONS),
            cards=sts2data.card_table(data, min_runs).to_html(**TABLE_OPTIONS),
        )

    @app.route("/u/<username>/runs")
    @login_required
    def user_runs(username: str):
        user = owned(username)
        tree = request.args.get("tree", "vanilla")
        character = request.args.get("character", "")
        result = request.args.get("result", "")

        # The character list is built before the character filter is applied,
        # so choosing one does not empty the dropdown.
        characters = models.distinct_characters(user.id, tree)

        rows = [
            {
                "run_id": r["start_time"],
                "date": datetime.fromtimestamp(r["start_time"]).strftime("%Y-%m-%d %H:%M"),
                "character": r.get("character"),
                "ascension": r.get("ascension"),
                "result": r.get("result"),
                "killed_by": r.get("killed_by") or "",
                "build": r.get("build"),
                "floors_climbed": r.get("floors"),
                "seed": r.get("seed"),
                "is_modded": r.get("is_modded", False),
            }
            for r in models.run_rows(user.id, tree, character, result)
        ]

        return render_template(
            "runs.html",
            user=user,
            characters=characters,
            character=character,
            result=result,
            tree=tree,
            run_count=len(rows),
            columns=sts2data.RUN_COLUMNS,
            rows=rows,
        )

    @app.route("/u/<username>/run/<int:run_id>")
    @login_required
    def run_detail(username: str, run_id: int):
        user = owned(username)
        run = models.find_run(user.id, run_id)
        if run is None:
            return render_template(
                "run.html",
                user=user,
                error=f"Run {run_id} is not in your uploads.",
            ), 404

        data = run["data"]
        return render_template(
            "run.html",
            user=user,
            is_modded=run.get("is_modded", False),
            summary=sts2data.run_summary(data),
            path_columns=sts2data.PATH_COLUMNS,
            path_rows=sts2data.run_path_table(data).to_dict("records"),
            deck=sts2data.deck_table(data).to_html(**TABLE_OPTIONS),
            relics=sts2data.relic_table(data).to_html(**TABLE_OPTIONS),
        )

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    def upload():
        if request.method == "GET":
            return render_template("upload.html")

        files = [f for f in request.files.getlist("files") if f and f.filename]
        if not files:
            flash("Choose at least one file.", "error")
            return render_template("upload.html"), 400
        if len(files) > uploads.MAX_FILES:
            flash(
                f"That is {len(files)} files. Upload at most {uploads.MAX_FILES} at once.",
                "error",
            )
            return render_template("upload.html"), 400

        force_modded = bool(request.form.get("modded"))
        results = ingest(files, current_user, force_modded)
        return render_template("upload.html", results=results), 200


def ingest(files, user, force_modded: bool) -> dict:
    """Validate and store an upload, reporting what happened to each file."""
    existing = models.existing_start_times(user.id)
    rows, added, duplicate, rejected, progress_saved, skipped = [], 0, 0, 0, 0, 0
    pending: list[dict] = []
    # Where each pending run's row sits, so it can be relabelled if the insert
    # turns out to have lost a race.
    row_of: dict[int, int] = {}

    for storage in files:
        parsed = uploads.parse_file(storage, force_modded=force_modded)

        if parsed.skipped:
            # A folder upload sends the whole save directory, most of which is
            # not run history. Not worth a row each.
            skipped += 1
            continue

        if not parsed.ok:
            rejected += 1
            rows.append((parsed.filename, "rejected", parsed.error))
            continue

        if parsed.kind == uploads.PROGRESS_KIND:
            models.save_progress_snapshot(user.id, parsed.is_modded, parsed.data)
            progress_saved += 1
            tree = "modded" if parsed.is_modded else "vanilla"
            rows.append((parsed.filename, "stored", f"lifetime totals ({tree})"))
            continue

        start_time = parsed.metadata["start_time"]
        # Covers both a re-upload and the same run appearing twice in one batch.
        if start_time in existing:
            duplicate += 1
            rows.append((parsed.filename, "duplicate", "already uploaded"))
            continue

        existing.add(start_time)
        row_of[start_time] = len(rows)
        pending.append(
            models.run_document(
                user.id, parsed.is_modded, parsed.data, parsed.metadata
            )
        )
        added += 1
        detail = f"{parsed.metadata['character']} {parsed.metadata['result']}"
        if parsed.is_modded:
            detail += " (modded)"
        rows.append((parsed.filename, "added", detail))

    # One insert rather than one commit: a standalone mongod has no
    # multi-document transactions, so a batch is not atomic. That is acceptable
    # because uploading is idempotent, so re-uploading repairs a partial batch.
    # Anything the unique index rejected was stored by a concurrent upload
    # between the duplicate check above and here, so it is reported as the
    # duplicate it is rather than as a failure.
    for start_time in models.insert_runs(pending):
        index = row_of[start_time]
        rows[index] = (rows[index][0], "duplicate", "already uploaded")
        added -= 1
        duplicate += 1

    return {
        "rows": rows,
        "added": added,
        "duplicate": duplicate,
        "rejected": rejected,
        "progress": progress_saved,
        "skipped": skipped,
    }


# Flask's CLI discovers create_app() automatically, so there is no module-level
# app. That also keeps importing this module free of side effects, which is what
# lets the tests build their own app with a throwaway config.
if __name__ == "__main__":
    create_app().run(debug=True)
