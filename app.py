"""Slay the Spire 2 stats dashboard.

    pip install -r requirements.txt
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
import sts2data
import uploads
from models import (
    ProgressSnapshot,
    Run,
    User,
    db,
    normalize_username,
)
from models import now as models_now
from models import password_error, username_error

TABLE_OPTIONS = {"index": False, "na_rep": "-", "classes": "stats", "border": 0}

csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = "index"
login_manager.login_message = "Log in to see that page."


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


def create_app(config: type[app_config.Config] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_object(app_config.resolve(config))

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    with app.app_context():
        app_config.INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        # Fine while the schema is young. Introduce Alembic once migrating a
        # populated database matters.
        db.create_all()
        check_schema()

    register_auth_routes(app)
    register_user_routes(app)
    return app


def check_schema() -> None:
    """Fail loudly when an existing database predates a model change.

    create_all() only creates missing tables, it never alters existing ones, so
    a column added to a model is silently absent until the table is rebuilt.
    Without this the first query dies with a bare "no such column".
    """
    inspector = db.inspect(db.engine)
    for model in (User, Run, ProgressSnapshot):
        table = model.__tablename__
        if not inspector.has_table(table):
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        missing = {c.name for c in model.__table__.columns} - present
        if missing:
            raise RuntimeError(
                f"Table '{table}' is missing {sorted(missing)}. The schema changed "
                f"and there are no migrations yet. Delete the database and upload "
                f"your saves again:\n"
                f"    rm instance/app.db"
            )


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
        if email and db.session.scalar(db.select(User).filter_by(email=email)):
            return reject("That email is already registered.")

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()

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

    def tree_filter(query, tree: str):
        """Modded runs are kept out of the numbers unless asked for."""
        if tree == "modded":
            return query.filter_by(is_modded=True)
        if tree == "all":
            return query
        return query.filter_by(is_modded=False)

    @app.route("/u/<username>")
    @login_required
    def overview(username: str):
        user = owned(username)
        tree = request.args.get("tree", "vanilla")
        min_runs = request.args.get("min_runs", default=5, type=int)

        counts = db.session.execute(
            db.select(Run.is_modded, db.func.count())
            .filter_by(user_id=user.id)
            .group_by(Run.is_modded)
        ).all()
        by_tree = {modded: n for modded, n in counts}

        # Every statistic is rebuilt from the run files on each request. That
        # means loading each run's JSON, which is fine for hundreds of runs and
        # wasteful for many thousands. Cache or precompute when that day comes.
        runs = list(
            db.session.scalars(
                tree_filter(db.select(Run).filter_by(user_id=user.id), tree)
            )
        )
        data = [r.data for r in runs]

        # progress.save covers runs the game has since pruned, so its totals can
        # exceed what was uploaded. That gap is the point of showing it.
        snapshot = db.session.scalar(
            db.select(ProgressSnapshot).filter_by(
                user_id=user.id, is_modded=(tree == "modded")
            )
        )
        lifetime = sts2data.lifetime_totals(snapshot.data) if snapshot else None
        loss = (
            sts2data.data_loss_report(snapshot.data, len(data)) if snapshot else None
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

        # Only the display columns: the JSON blob is big and the list does not
        # need it.
        columns = (
            Run.start_time, Run.character, Run.ascension, Run.result,
            Run.killed_by, Run.build, Run.floors, Run.seed, Run.is_modded,
        )
        query = tree_filter(db.select(*columns).filter_by(user_id=user.id), tree)

        # The character list is built before the character filter is applied,
        # so choosing one does not empty the dropdown.
        characters = sorted(
            c for c in db.session.scalars(
                tree_filter(
                    db.select(Run.character).filter_by(user_id=user.id).distinct(), tree
                )
            ) if c
        )

        if character:
            query = query.filter(Run.character == character)
        if result:
            query = query.filter(Run.result == result)

        rows = [
            {
                "run_id": r.start_time,
                "date": datetime.fromtimestamp(r.start_time).strftime("%Y-%m-%d %H:%M"),
                "character": r.character,
                "ascension": r.ascension,
                "result": r.result,
                "killed_by": r.killed_by or "",
                "build": r.build,
                "floors_climbed": r.floors,
                "seed": r.seed,
                "is_modded": r.is_modded,
            }
            for r in db.session.execute(query.order_by(Run.start_time.desc())).all()
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
        run = db.session.scalar(
            db.select(Run).filter_by(user_id=user.id, start_time=run_id)
        )
        if run is None:
            return render_template(
                "run.html",
                user=user,
                error=f"Run {run_id} is not in your uploads.",
            ), 404

        return render_template(
            "run.html",
            user=user,
            is_modded=run.is_modded,
            summary=sts2data.run_summary(run.data),
            path_columns=sts2data.PATH_COLUMNS,
            path_rows=sts2data.run_path_table(run.data).to_dict("records"),
            deck=sts2data.deck_table(run.data).to_html(**TABLE_OPTIONS),
            relics=sts2data.relic_table(run.data).to_html(**TABLE_OPTIONS),
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
    existing = set(
        db.session.scalars(
            db.select(Run.start_time).filter_by(user_id=user.id)
        ).all()
    )
    rows, added, duplicate, rejected, progress_saved, skipped = [], 0, 0, 0, 0, 0

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
            snapshot = db.session.scalar(
                db.select(ProgressSnapshot).filter_by(
                    user_id=user.id, is_modded=parsed.is_modded
                )
            )
            if snapshot is None:
                snapshot = ProgressSnapshot(user_id=user.id, is_modded=parsed.is_modded)
                db.session.add(snapshot)
            snapshot.data = parsed.data
            snapshot.uploaded_at = models_now()
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
        db.session.add(
            Run(
                user_id=user.id,
                is_modded=parsed.is_modded,
                data=parsed.data,
                **parsed.metadata,
            )
        )
        added += 1
        detail = f"{parsed.metadata['character']} {parsed.metadata['result']}"
        if parsed.is_modded:
            detail += " (modded)"
        rows.append((parsed.filename, "added", detail))

    db.session.commit()
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
