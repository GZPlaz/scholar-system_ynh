"""Scholar System — standards-based assessment and mastery tracking for MindAccel University."""
import logging
import secrets

import click
from flask import Flask, g, render_template, request
from flask_login import current_user
from markupsafe import Markup, escape
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import build_config
from .extensions import csrf, db, login_manager

__version__ = "1.2.0"
SCHEMA_VERSION = 3  # 2: answer_sheets, sheet_scans; 3: api_tokens, users/courses.external_id


class PrefixMiddleware:
    """Serve the app under a sub-path (YunoHost ``path`` setting) behind nginx."""

    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix.rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["PATH_INFO"] = path[len(self.prefix) :] or "/"
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(build_config())
    if overrides:
        app.config.update(overrides)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    db.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"

    if app.config["TRUST_PROXY"]:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    if app.config["APP_PREFIX"]:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, app.config["APP_PREFIX"])

    from . import auth
    from .views import admin, api, assessments, courses, main, questions, reports, scans, standards, student

    for module in (auth, main, admin, courses, standards, questions, assessments, scans, student, reports, api):
        app.register_blueprint(module.bp)

    app.before_request(auth.try_sso_login)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.endpoint and request.endpoint.startswith("student."):
            response.headers["Cache-Control"] = "no-store"
        return response

    return app


def _register_template_helpers(app):
    from .analytics import LEVEL_LABELS, level_for
    from .models import QTYPES, ROLES, SHOW_RESULTS
    from .utils import can_manage_course, fmt_num, format_dt, get_setting, to_input_value

    @app.template_filter("dt")
    def dt_filter(value, fmt="%b %d, %Y %H:%M"):
        return format_dt(value, fmt)

    @app.template_filter("num")
    def num_filter(value, digits=2):
        return fmt_num(value, digits)

    @app.template_filter("pct")
    def pct_filter(value, digits=1):
        return "—" if value is None else f"{value:.{digits}f}%"

    @app.template_filter("richtext")
    def richtext(value):
        """Escape user content and keep line breaks; support `code` and **bold**."""
        import re

        text = str(escape(value or ""))
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        paragraphs = [p.replace("\n", "<br>") for p in re.split(r"\n\s*\n", text.strip())]
        return Markup("".join(f"<p>{p}</p>" for p in paragraphs if p))

    @app.template_filter("letter")
    def letter(index):
        return chr(65 + int(index))

    @app.context_processor
    def inject_globals():
        return {
            "institution_name": get_setting("institution_name"),
            "login_message": get_setting("login_message") if request.endpoint == "auth.login" else "",
            "app_version": __version__,
            "QTYPES": QTYPES,
            "ROLES": ROLES,
            "SHOW_RESULTS": SHOW_RESULTS,
            "LEVEL_LABELS": LEVEL_LABELS,
            "level_for": level_for,
            "can_manage_course": can_manage_course,
            "to_input_value": to_input_value,
            "sso_enabled": app.config["SSO_ENABLED"],
            "ldap_enabled": app.config["LDAP_ENABLED"],
        }


def _register_error_handlers(app):
    def render_error(code, title, message):
        return render_template("error.html", code=code, title=title, message=message), code

    app.register_error_handler(403, lambda e: render_error(403, "Access denied", "You don't have permission to view this page."))
    app.register_error_handler(404, lambda e: render_error(404, "Not found", "The page you requested does not exist."))
    app.register_error_handler(400, lambda e: render_error(400, "Bad request", getattr(e, "description", "") or "The request could not be processed."))
    app.register_error_handler(413, lambda e: render_error(413, "File too large", "Uploads are limited to 25 MB."))

    @app.errorhandler(500)
    def server_error(e):
        db.session.rollback()
        return render_error(500, "Server error", "Something went wrong. The error has been logged.")


def upgrade_schema():
    """Create missing tables and apply incremental migrations."""
    from .models import Setting

    db.create_all()
    row = db.session.get(Setting, "schema_version")
    current = int(row.value) if row and row.value.isdigit() else 0
    # New tables come from create_all(); new columns on existing tables are added here.
    _add_missing_column("users", "external_id", "VARCHAR(120)")
    _add_missing_column("courses", "external_id", "VARCHAR(120)")
    if current != SCHEMA_VERSION:
        if row is None:
            db.session.add(Setting(key="schema_version", value=str(SCHEMA_VERSION)))
        else:
            row.value = str(SCHEMA_VERSION)
    db.session.commit()
    return current


def _add_missing_column(table, column, ddl_type):
    """ALTER TABLE ... ADD COLUMN when an upgraded database lacks a column (SQLite and PostgreSQL)."""
    inspector = db.inspect(db.engine)
    if column in {c["name"] for c in inspector.get_columns(table)}:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        conn.execute(db.text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))


def _register_cli(app):
    from .models import User

    @app.cli.command("init-db")
    @click.option("--admin", "admin_username", default="", help="YunoHost username that becomes administrator")
    @click.option("--institution", default="", help="Institution name")
    @click.option("--timezone", "tz", default="", help="IANA timezone, e.g. Europe/Paris")
    def init_db(admin_username, institution, tz):
        """Create tables and the initial administrator."""
        from .utils import set_setting

        upgrade_schema()
        if institution:
            set_setting("institution_name", institution)
        if tz:
            set_setting("timezone", tz)
        if admin_username:
            user = User.query.filter_by(username=admin_username).first()
            if user is None:
                user = User(username=admin_username, auth_source="ldap", role="admin")
                db.session.add(user)
            user.role = "admin"
        db.session.commit()
        click.echo("Database initialised.")

    @app.cli.command("upgrade-db")
    def upgrade_db():
        """Apply schema upgrades (run on every YunoHost upgrade)."""
        before = upgrade_schema()
        click.echo(f"Schema at version {SCHEMA_VERSION} (was {before}).")

    @app.cli.command("create-local-admin")
    @click.argument("username")
    @click.option("--password", default="", help="Leave empty to generate one")
    def create_local_admin(username, password):
        """Create (or reset) a local administrator account with a password."""
        password = password or secrets.token_urlsafe(12)
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username, full_name=username)
            db.session.add(user)
        user.role = "admin"
        user.auth_source = "local"
        user.active = True
        user.must_change_password = True
        user.set_password(password)
        db.session.commit()
        click.echo(f"Local administrator '{username}' ready. Temporary password: {password}")

    @app.cli.command("create-api-token")
    @click.argument("name")
    def create_api_token(name):
        """Create an integration API token (printed once)."""
        from .models import ApiToken
        from .views.api import hash_token, new_token

        secret, prefix = new_token()
        db.session.add(ApiToken(name=name[:120], prefix=prefix, token_hash=hash_token(secret)))
        db.session.commit()
        click.echo(secret)

    @app.cli.command("seed-demo")
    def seed_demo():
        """Load a small demo course with standards, questions and students."""
        from .demo import seed

        seed()
        click.echo("Demo data loaded. Demo student passwords: 'student123'.")
