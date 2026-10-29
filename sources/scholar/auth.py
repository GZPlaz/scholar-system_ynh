"""Authentication: local accounts, YunoHost LDAP accounts and YunoHost SSO (SSOwat) headers."""
import base64
import logging
import re

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from .extensions import db, login_manager
from .models import ROLES, User, utcnow
from .utils import get_setting, log_event, parse_bool

bp = Blueprint("auth", __name__)
log = logging.getLogger("scholar.auth")

# nginx copies the SSOwat-provided identity into these headers after stripping any
# client-supplied value (see conf/nginx.conf), so they can be trusted.
SSO_HEADERS = ("X-Scholar-User", "X-Scholar-Ynh-User")


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- LDAP


def _ldap_escape_dn(value):
    from ldap3.utils.dn import escape_rdn

    return escape_rdn(value)


def ldap_authenticate(username, password):
    """Bind against YunoHost's slapd as the user. Returns {'full_name', 'email'} or None."""
    if not current_app.config["LDAP_ENABLED"] or not username or not password:
        return None
    try:
        from ldap3 import Connection, Server
        from ldap3.core.exceptions import LDAPException
        from ldap3.utils.conv import escape_filter_chars
    except ImportError:  # pragma: no cover
        log.error("ldap3 is not installed")
        return None
    dn = f"uid={_ldap_escape_dn(username)},{current_app.config['LDAP_USER_BASE']}"
    try:
        server = Server(current_app.config["LDAP_URI"], connect_timeout=5)
        conn = Connection(server, user=dn, password=password, receive_timeout=10)
        if not conn.bind():
            return None
        info = {"full_name": "", "email": ""}
        conn.search(
            current_app.config["LDAP_USER_BASE"],
            f"(uid={escape_filter_chars(username)})",
            attributes=["cn", "displayName", "mail"],
        )
        if conn.entries:
            entry = conn.entries[0]
            attrs = entry.entry_attributes_as_dict
            name = (attrs.get("displayName") or attrs.get("cn") or [""])[0]
            mail = (attrs.get("mail") or [""])[0]
            info = {"full_name": str(name), "email": str(mail)}
        conn.unbind()
        return info
    except LDAPException as exc:
        log.warning("LDAP error while authenticating %s: %s", username, exc)
        return None


def ldap_lookup(username):
    """Anonymous lookup of a YunoHost user's name/email (best effort)."""
    if not current_app.config["LDAP_ENABLED"]:
        return {}
    try:
        from ldap3 import Connection, Server
        from ldap3.utils.conv import escape_filter_chars

        conn = Connection(Server(current_app.config["LDAP_URI"], connect_timeout=5), receive_timeout=10)
        if not conn.bind():
            return {}
        conn.search(
            current_app.config["LDAP_USER_BASE"],
            f"(uid={escape_filter_chars(username)})",
            attributes=["cn", "displayName", "mail"],
        )
        if not conn.entries:
            return {}
        attrs = conn.entries[0].entry_attributes_as_dict
        return {
            "full_name": str((attrs.get("displayName") or attrs.get("cn") or [""])[0]),
            "email": str((attrs.get("mail") or [""])[0]),
        }
    except Exception as exc:  # noqa: BLE001 - lookup is best effort only
        log.info("Anonymous LDAP lookup failed for %s: %s", username, exc)
        return {}


def provision_directory_user(username, info):
    """Find or create the local record for a YunoHost (LDAP/SSO) user. May return None."""
    user = User.query.filter(db.func.lower(User.username) == username.lower()).first()
    admin_name = (current_app.config.get("ADMIN_USERNAME") or "").lower()
    if user is None:
        if not parse_bool(get_setting("ldap_autoprovision")) and username.lower() != admin_name:
            return None
        role = get_setting("ldap_default_role") or "student"
        if role not in ROLES:
            role = "student"
        if username.lower() == admin_name:
            role = "admin"
        user = User(username=username, role=role, auth_source="ldap", active=True)
        db.session.add(user)
        db.session.flush()
        log_event("user.provisioned", f"{username} ({role}) from YunoHost directory")
    if info.get("full_name") and not user.full_name:
        user.full_name = info["full_name"]
    if info.get("email") and not user.email:
        user.email = info["email"]
    return user


def _finish_login(user, method):
    user.last_login_at = utcnow()
    login_user(user)
    session.permanent = True
    session["auth_method"] = method
    session.pop("sso_suppressed", None)
    log_event("login", method)
    db.session.commit()
    log.info("Successful login for %s via %s from %s", user.username, method, request.remote_addr)


def sso_username():
    if not current_app.config["SSO_ENABLED"]:
        return None
    for header in SSO_HEADERS:
        value = (request.headers.get(header) or "").strip()
        if value:
            return value
    return None


def try_sso_login():
    """Called before each request: log the user in from SSOwat headers if possible."""
    if current_user.is_authenticated or session.get("sso_suppressed"):
        return
    if request.endpoint in ("static", "auth.logout"):
        return
    username = sso_username()
    if not username:
        return
    user = provision_directory_user(username, ldap_lookup(username))
    if user is None or not user.active:
        db.session.rollback()
        return
    _finish_login(user, "sso")


# --------------------------------------------------------------------------- views


def _safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    next_url = _safe_next(request.values.get("next"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(db.func.lower(User.username) == username.lower()).first() if username else None
        authenticated = None
        if user is not None and user.auth_source == "local":
            if user.check_password(password):
                authenticated = user
        elif username:
            info = ldap_authenticate(username, password)
            if info is not None:
                authenticated = provision_directory_user(username, info)
                if authenticated is None:
                    flash("Your YunoHost account is not registered in Scholar. Ask an administrator.", "error")
                    db.session.rollback()
                    return render_template("auth/login.html", next=next_url), 403
        if authenticated is not None and not authenticated.active:
            authenticated = None
        if authenticated is None:
            # this exact wording is matched by the fail2ban filter installed on YunoHost
            # (the username is sanitised so it cannot forge extra log lines for fail2ban)
            shown = re.sub(r"[^\w.@-]", "?", username)[:64] or "-"
            log.warning("Failed login attempt for %s from %s", shown, request.remote_addr)
            flash("Invalid username or password.", "error")
            return render_template("auth/login.html", next=next_url, username=username), 401
        _finish_login(authenticated, "ldap" if authenticated.auth_source == "ldap" else "password")
        if authenticated.must_change_password:
            flash("Please choose a new password before continuing.", "info")
            return redirect(url_for("main.account"))
        return redirect(next_url or url_for("main.dashboard"))
    return render_template("auth/login.html", next=next_url)


@bp.route("/login/sso")
def login_sso():
    session.pop("sso_suppressed", None)
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    try_sso_login()
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    if not current_app.config["SSO_ENABLED"]:
        flash("YunoHost single sign-on is not enabled on this server.", "error")
        return redirect(url_for("auth.login"))
    # Not logged into the YunoHost portal yet: send the user there and come back.
    back = request.url_root.rstrip("/") + url_for("auth.login_sso")
    target = base64.b64encode(back.encode()).decode()
    return redirect(f"{current_app.config['SSO_LOGIN_URL']}?r={target}")


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    method = session.get("auth_method")
    log_event("logout")
    db.session.commit()
    logout_user()
    session.clear()
    if method == "sso":
        # Otherwise the SSOwat header would sign the user straight back in.
        session["sso_suppressed"] = True
        flash("You are signed out of Scholar. Your YunoHost portal session is still open.", "info")
    else:
        flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))
