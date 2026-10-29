"""Configuration loading.

On YunoHost the install script writes ``scholar.env`` next to the application
directory (``/var/www/<app>/scholar.env``).  Values from the real environment
always win over the file so the service can be tuned with systemd overrides.
"""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = APP_DIR.parent / "scholar.env"


def load_env_file(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def build_config():
    load_env_file(os.environ.get("SCHOLAR_CONFIG", str(DEFAULT_ENV_FILE)))
    prefix = os.environ.get("SCHOLAR_PATH", "/").rstrip("/")
    instance = os.environ.get("SCHOLAR_INSTANCE", "scholar")
    database_url = os.environ.get("SCHOLAR_DATABASE_URL", "sqlite:///" + str(APP_DIR / "scholar-dev.sqlite3"))
    engine_options = {"pool_pre_ping": True}
    if not database_url.startswith("sqlite"):
        # several gunicorn workers × threads share PostgreSQL with the server's other apps
        engine_options.update({"pool_size": 4, "max_overflow": 4, "pool_recycle": 1800})
    return {
        "SECRET_KEY": os.environ.get("SCHOLAR_SECRET_KEY", "dev-insecure-change-me"),
        "SQLALCHEMY_DATABASE_URI": database_url,
        "SQLALCHEMY_ENGINE_OPTIONS": engine_options,
        "APP_PREFIX": prefix,
        "APPLICATION_ROOT": prefix or "/",
        "INSTANCE_NAME": instance,
        "SESSION_COOKIE_NAME": f"{instance}_session",
        "SESSION_COOKIE_SECURE": _bool("SCHOLAR_SECURE_COOKIES", False),
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "PERMANENT_SESSION_LIFETIME": 60 * 60 * 12,
        # Exams can last hours: never expire the CSRF token while the page is open.
        "WTF_CSRF_TIME_LIMIT": None,
        "MAX_CONTENT_LENGTH": 25 * 1024 * 1024,  # phone photos of answer sheets
        "LDAP_ENABLED": _bool("SCHOLAR_LDAP_ENABLED", False),
        "LDAP_URI": os.environ.get("SCHOLAR_LDAP_URI", "ldap://127.0.0.1:389"),
        "LDAP_USER_BASE": os.environ.get("SCHOLAR_LDAP_USER_BASE", "ou=users,dc=yunohost,dc=org"),
        "SSO_ENABLED": _bool("SCHOLAR_SSO_ENABLED", False),
        "SSO_LOGIN_URL": os.environ.get("SCHOLAR_SSO_LOGIN_URL", "/yunohost/sso/"),
        "ADMIN_USERNAME": os.environ.get("SCHOLAR_ADMIN", ""),
        "DEFAULT_INSTITUTION": os.environ.get("SCHOLAR_INSTITUTION", "MindAccel University"),
        "DEFAULT_TIMEZONE": os.environ.get("SCHOLAR_TIMEZONE", "UTC"),
        "TRUST_PROXY": _bool("SCHOLAR_TRUST_PROXY", True),
    }
