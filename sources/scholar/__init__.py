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

__version__ = "1.7.0"
STAFF_PREFIX = "/staff"
# 2: answer_sheets, sheet_scans; 3: api_tokens, users/courses.external_id;
# 4: test portal (test_sessions, attempt_events, portal_failures, PINs, security settings);
# 5: lockdown browser, access passwords, one sitting, restricted navigation, accommodations;
# 6: tests made from a PDF (assessment_pdfs, assessment_pdf_pages)
SCHEMA_VERSION = 6


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
    from .views import admin, api, assessments, courses, legacy, main, pdftests, portal, proctor, questions, reports, scans, standards

    # The site root is the student test portal; everything for instructors and administrators
    # lives under /staff. The integration API keeps its own /api/v1 so connected systems
    # (the RosarioSIS plugin) don't depend on the staff layout.
    for module in (auth, main, admin, courses, standards, questions, assessments, pdftests, proctor, scans, reports):
        app.register_blueprint(module.bp, url_prefix=STAFF_PREFIX + (module.bp.url_prefix or ""))
    app.register_blueprint(api.bp)
    app.register_blueprint(portal.bp)
    app.register_blueprint(legacy.bp)

    app.before_request(auth.try_sso_login)
    app.before_request(auth.keep_students_out)
    _register_template_helpers(app)
    _register_error_handlers(app)
    _register_cli(app)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        endpoint = request.endpoint or ""
        if endpoint.startswith("portal.") or endpoint == "assessments.preview":
            # The test portal: nothing cached, nothing framed, no third-party anything, no
            # inline scripts, and the browser features a test never needs switched off.
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["X-Frame-Options"] = "DENY"
            # Not "no-referrer": over HTTPS the form protection rejects a POST without a Referer
            # ("The referrer header is missing"). Same-origin still sends nothing to other sites.
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; "
                "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
            )
            response.headers["Permissions-Policy"] = (
                "camera=(), microphone=(), geolocation=(), payment=(), usb=(), display-capture=(), clipboard-read=()"
            )
        return response

    return app


def _register_template_helpers(app):
    from .analytics import LEVEL_LABELS, level_for
    from .models import EVENT_ATTENTION, EVENT_LABELS, IDENTITY_CHECKS, QTYPES, ROLES, SHOW_RESULTS
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
            "EVENT_LABELS": EVENT_LABELS,
            "EVENT_ATTENTION": EVENT_ATTENTION,
            "IDENTITY_CHECKS": IDENTITY_CHECKS,
            "LEVEL_LABELS": LEVEL_LABELS,
            "level_for": level_for,
            "can_manage_course": can_manage_course,
            "to_input_value": to_input_value,
            "sso_enabled": app.config["SSO_ENABLED"],
            "ldap_enabled": app.config["LDAP_ENABLED"],
        }


def _register_error_handlers(app):
    def render_error(code, title, message):
        home = "portal" if (request.endpoint or "").startswith("portal.") else "staff"
        return render_template("error.html", code=code, title=title, message=message, home=home), code

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
    for table, column, ddl in SCHEMA_4_COLUMNS:
        _add_missing_column(table, column, ddl, index=False)
    if 0 < current < 4:
        _migrate_to_portal()
    _ensure_password_secrets()
    _ensure_test_codes()
    if current != SCHEMA_VERSION:
        if row is None:
            db.session.add(Setting(key="schema_version", value=str(SCHEMA_VERSION)))
        else:
            row.value = str(SCHEMA_VERSION)
    db.session.commit()
    return current


SCHEMA_4_COLUMNS = [
    ("users", "pin_hash", "VARCHAR(256)"),
    ("users", "pin_set_at", "TIMESTAMP"),
    ("assessments", "identity_check", "VARCHAR(20) NOT NULL DEFAULT 'confirm'"),
    ("assessments", "require_approval", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("assessments", "lock_to_device", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("assessments", "require_fullscreen", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("assessments", "block_copy_paste", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("assessments", "max_focus_losses", "INTEGER"),
    ("assessments", "allowed_networks", "TEXT NOT NULL DEFAULT ''"),
    ("assessments", "late_start_minutes", "INTEGER"),
    ("assessments", "watermark", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("attempts", "extra_minutes", "INTEGER NOT NULL DEFAULT 0"),
    ("attempts", "locked", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("attempts", "lock_reason", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("attempts", "focus_losses", "INTEGER NOT NULL DEFAULT 0"),
    ("attempts", "ip", "VARCHAR(64) NOT NULL DEFAULT ''"),
    # schema 5
    ("assessments", "lockdown_browser", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("assessments", "seb_quit_password_hash", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("assessments", "seb_extra_keys", "TEXT NOT NULL DEFAULT ''"),
    ("assessments", "seb_only_listed_keys", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("assessments", "access_password_mode", "VARCHAR(20) NOT NULL DEFAULT ''"),
    ("assessments", "access_password", "VARCHAR(60) NOT NULL DEFAULT ''"),
    ("assessments", "password_secret", "VARCHAR(64) NOT NULL DEFAULT ''"),
    ("assessments", "password_rotation_minutes", "INTEGER NOT NULL DEFAULT 5"),
    ("assessments", "bypass_password_hash", "VARCHAR(256) NOT NULL DEFAULT ''"),
    ("assessments", "one_sitting", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("assessments", "sequential", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("assessments", "focus_grace_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ("assessments", "clock_stops_when_paused", "BOOLEAN NOT NULL DEFAULT FALSE"),
    ("attempts", "locked_at", "TIMESTAMP"),
    ("attempts", "position", "INTEGER NOT NULL DEFAULT 0"),
    ("test_sessions", "lockdown_ok", "BOOLEAN NOT NULL DEFAULT FALSE"),
]


def _add_missing_column(table, column, ddl_type, index=True):
    """ALTER TABLE ... ADD COLUMN when an upgraded database lacks a column (SQLite and PostgreSQL)."""
    inspector = db.inspect(db.engine)
    if column in {c["name"] for c in inspector.get_columns(table)}:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))
        if index:
            conn.execute(db.text(f"CREATE INDEX IF NOT EXISTS ix_{table}_{column} ON {table} ({column})"))


def _migrate_to_portal():
    """Version 4: students stop having accounts on the staff site; they use the test portal."""
    from .models import Setting, User

    for user in User.query.filter_by(role="student"):
        user.password_hash = None
        user.must_change_password = False
        user.auth_source = "portal"
    # Unknown YunoHost users used to become students. Students can no longer sign in, so stop
    # creating accounts for them; administrators add instructors explicitly.
    role = db.session.get(Setting, "ldap_default_role")
    if role is None or role.value != "instructor":
        for key, value in (("ldap_autoprovision", "0"), ("ldap_default_role", "instructor")):
            setting = db.session.get(Setting, key)
            if setting is None:
                db.session.add(Setting(key=key, value=value))
            else:
                setting.value = value


def _ensure_password_secrets():
    from .models import Assessment

    for assessment in Assessment.query.filter(Assessment.password_secret == ""):
        assessment.password_secret = secrets.token_hex(32)


def _ensure_test_codes():
    """Every assessment gets a unique test code; short or duplicated legacy access codes are replaced."""
    from .models import Assessment
    from .security import CUSTOM_CODE_MIN, generate_code, normalize_code

    seen = set()
    for assessment in Assessment.query.order_by(Assessment.id):
        code = normalize_code(assessment.access_code)
        if len(code) < CUSTOM_CODE_MIN or code in seen:
            db.session.flush()
            code = generate_code()
        if code != assessment.access_code:
            assessment.access_code = code
        seen.add(code)


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
        click.echo("Demo data loaded. Students take tests in the test portal (the site root) with the test code and their student ID.")
