"""Slay the Spire 2 stats dashboard.

    pip install -r requirements.txt
    export FLASK_DEBUG=1
    flask --app app run --debug

For anything other than local development, set a real secret key:

    python -c 'import secrets; print(secrets.token_hex(32))'
    export SECRET_KEY=...
"""

from __future__ import annotations

from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_wtf.csrf import CSRFProtect

import config as app_config
import sts2data
from models import User, db, normalize_username, password_error, username_error

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

    register_auth_routes(app)
    register_user_routes(app)
    register_legacy_routes(app)
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
    @app.route("/u/<username>")
    @login_required
    def overview(username: str):
        # Profiles are private. A 403 here would confirm the account exists, so
        # anything that is not yours looks the same as a name nobody has taken.
        if normalize_username(username) != current_user.username:
            abort(404)
        return render_template("overview.html", user=current_user)


# --------------------------------------------------------------------------
# legacy local-file routes
#
# These still read the save folder of whoever is running the server. They are
# replaced by per-user pages backed by uploads once phase 3 lands, and are kept
# working until then so the dashboard stays usable.
# --------------------------------------------------------------------------


def register_legacy_routes(app: Flask) -> None:
    def selected_profile(profiles: list[dict]) -> dict:
        wanted = request.args.get("saves")
        for profile in profiles:
            if profile["saves"] == wanted:
                return profile
        return sts2data.default_profile(profiles)

    def archive_dir_for(profile: dict):
        return sts2data.ARCHIVE / profile["label"].replace("/", "_")

    @app.route("/local")
    @login_required
    def local_stats():
        profiles = sts2data.find_profiles()
        if not profiles:
            return render_template(
                "local.html", error=f"No save data found under {sts2data.BASE}"
            )

        profile = selected_profile(profiles)
        progress = sts2data.load_progress(profile["saves"])
        min_runs = request.args.get("min_runs", default=5, type=int)

        return render_template(
            "local.html",
            profiles=profiles,
            profile=profile,
            min_runs=min_runs,
            totals=sts2data.totals(progress),
            loss=sts2data.data_loss_report(progress, profile["saves"]),
            archived=sts2data.archive_runs(profile["saves"], profile["label"]),
            characters=sts2data.character_table(progress).to_html(**TABLE_OPTIONS),
            cards=sts2data.card_table(progress, min_runs).to_html(**TABLE_OPTIONS),
        )

    @app.route("/local/api/progress")
    @login_required
    def api_progress():
        profiles = sts2data.find_profiles()
        if not profiles:
            return jsonify(error="No save data found"), 404
        return jsonify(sts2data.load_progress(selected_profile(profiles)["saves"]))

    @app.route("/local/runs")
    @login_required
    def runs():
        profiles = sts2data.find_profiles()
        if not profiles:
            return render_template(
                "runs.html", error=f"No save data found under {sts2data.BASE}"
            )

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

    @app.route("/local/run/<int:run_id>")
    @login_required
    def run_detail(run_id: int):
        profiles = sts2data.find_profiles()
        if not profiles:
            return render_template(
                "run.html", error=f"No save data found under {sts2data.BASE}"
            ), 404

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


# Flask's CLI discovers create_app() automatically, so there is no module-level
# app. That also keeps importing this module free of side effects, which is what
# lets the tests build their own app with a throwaway config.
if __name__ == "__main__":
    create_app().run(debug=True)
