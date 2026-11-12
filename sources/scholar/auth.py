"""Authentication: local accounts, YunoHost LDAP accounts and YunoHost SSO (SSOwat) headers."""
import base64
import logging
import re
import time

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import staffsec
from .extensions import db, login_manager
from .models import User, utcnow
from .utils import get_setting, log_event, parse_bool, parse_int

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
        # Unknown YunoHost users only get an account if the administrator allowed it, and then as
        # instructors: students never have accounts on the staff site.
        autoprovision = parse_bool(get_setting("ldap_autoprovision")) and get_setting("ldap_default_role") == "instructor"
        if not autoprovision and username.lower() != admin_name:
            return None
        role = "admin" if username.lower() == admin_name else "instructor"
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
    session.clear()  # nothing from before the sign-in survives it
    login_user(user)
    session.permanent = True
    session["auth_method"] = method
    session["sv"] = user.session_version or 0
    session["seen"] = int(time.time())
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
    endpoint = request.endpoint or ""
    if endpoint in ("static", "auth.logout") or endpoint.startswith(("api.", "portal.")):
        # the integration API uses tokens and the test portal its own sessions, never portal logins
        return
    username = sso_username()
    if not username:
        return
    user = provision_directory_user(username, ldap_lookup(username))
    if user is None or not user.can_sign_in:
        db.session.rollback()
        return
    _finish_login(user, "sso")


def guard_session():
    """Before each staff request: end sessions that were revoked or left idle, and hold staff who
    must set up two-step sign-in on that page."""
    if not current_user.is_authenticated:
        return None
    endpoint = request.endpoint or ""
    if endpoint == "static" or endpoint.startswith(("api.", "portal.")):
        return None
    now = int(time.time())
    idle = parse_int(get_setting("idle_timeout_minutes"), 0) or 0
    revoked = session.get("sv", 0) != (current_user.session_version or 0)
    sso = session.get("auth_method") == "sso"
    # (the YunoHost portal decides how long its own sign-ins last)
    timed_out = bool(idle) and not sso and now - session.get("seen", now) > idle * 60
    if revoked or timed_out:
        logout_user()
        session.clear()
        if sso:
            session["sso_suppressed"] = True
        flash("You were signed out. Please sign in again." if revoked else "You were signed out after a period without activity.", "info")
        return redirect(url_for("auth.login", next=request.full_path if request.method == "GET" else None))
    session["seen"] = now  # (the live proctor page counts as activity: a proctor is never signed out mid-test)
    if (
        parse_bool(get_setting("require_two_step"))
        and not current_user.totp_enabled
        and session.get("auth_method") != "sso"  # the YunoHost portal vouches for those
        and endpoint not in ("main.account", "main.two_step", "auth.logout")
    ):
        flash("Two-step sign-in is required for staff accounts. Set it up to continue.", "info")
        return redirect(url_for("main.two_step"))
    return None


def keep_students_out():
    """Students (and deactivated users) can't hold a staff-site session, even one from before an upgrade."""
    if not current_user.is_authenticated or current_user.can_sign_in:
        return None
    endpoint = request.endpoint or ""
    if endpoint == "static" or endpoint.startswith(("api.", "portal.")):
        return None
    logout_user()
    session.clear()
    session["sso_suppressed"] = True
    return redirect(url_for("portal.enter"))


# --------------------------------------------------------------------------- views


def _safe_next(target):
    # Only paths on this site: "//host" and "/\\host" are read by browsers as other sites.
    if target and target.startswith("/") and not target.startswith(("//", "/\\")) and not any(ch in target for ch in "\r\n\t"):
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
        wait = staffsec.throttled(username, request.remote_addr)
        if wait:
            log.warning("Sign-in for %s from %s refused: too many attempts", re.sub(r"[^\w.@-]", "?", username)[:64] or "-", request.remote_addr)
            flash(f"Too many sign-in attempts. Try again in {wait} minute{'s' if wait != 1 else ''}.", "error")
            return render_template("auth/login.html", next=next_url, username=username), 429
        user = User.query.filter(db.func.lower(User.username) == username.lower()).first() if username else None
        authenticated = None
        if user is not None and user.auth_source == "local":
            if user.check_password(password):
                authenticated = user
            elif not user.password_hash:
                staffsec.burn_time(password)
        elif username:
            if user is None and not current_app.config["LDAP_ENABLED"]:
                staffsec.burn_time(password)  # same time as a real check: usernames can't be probed
            info = ldap_authenticate(username, password)
            if info is not None:
                authenticated = provision_directory_user(username, info)
                if authenticated is None:
                    flash("Your YunoHost account is not registered in Scholar. Ask an administrator.", "error")
                    db.session.rollback()
                    return render_template("auth/login.html", next=next_url), 403
        if authenticated is not None and authenticated.active and authenticated.is_student:
            db.session.rollback()
            log.info("Student %s was sent to the test portal", authenticated.username)
            return render_template("auth/login.html", next=next_url, student_redirect=True), 403
        if authenticated is not None and not authenticated.active:
            authenticated = None
        if authenticated is None:
            # this exact wording is matched by the fail2ban filter installed on YunoHost
            # (the username is sanitised so it cannot forge extra log lines for fail2ban)
            shown = re.sub(r"[^\w.@-]", "?", username)[:64] or "-"
            log.warning("Failed login attempt for %s from %s", shown, request.remote_addr)
            db.session.rollback()
            staffsec.record_failure(username, request.remote_addr)
            db.session.commit()
            flash("Invalid username or password.", "error")
            return render_template("auth/login.html", next=next_url, username=username), 401
        method = "ldap" if authenticated.auth_source == "ldap" else "password"
        if authenticated.totp_enabled:
            # The password was right; the sign-in only happens after the second step.
            db.session.commit()
            session.clear()
            session["second_step"] = {"uid": authenticated.id, "method": method, "next": next_url, "at": int(time.time())}
            return redirect(url_for("auth.second_step"))
        staffsec.clear_failures(username, request.remote_addr)
        _finish_login(authenticated, method)
        if authenticated.must_change_password:
            flash("Please choose a new password before continuing.", "info")
            return redirect(url_for("main.account"))
        return redirect(next_url or url_for("main.dashboard"))
    return render_template("auth/login.html", next=next_url)


SECOND_STEP_MINUTES = 5


@bp.route("/login/verify", methods=["GET", "POST"])
def second_step():
    """The code from the authenticator app (or a recovery code), after a correct password."""
    pending = session.get("second_step")
    user = db.session.get(User, pending["uid"]) if pending else None
    if (
        user is None
        or not user.totp_enabled
        or not user.can_sign_in
        or time.time() - pending.get("at", 0) > SECOND_STEP_MINUTES * 60
    ):
        session.pop("second_step", None)
        if pending:
            flash("That took too long. Please sign in again.", "info")
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        wait = staffsec.throttled(user.username, request.remote_addr)
        if wait:
            session.pop("second_step", None)
            flash(f"Too many sign-in attempts. Try again in {wait} minute{'s' if wait != 1 else ''}.", "error")
            return redirect(url_for("auth.login"))
        how = staffsec.check_second_step(user, request.form.get("code", ""))
        if how is None:
            # same wording as a wrong password, so fail2ban counts wrong codes too
            log.warning("Failed login attempt for %s from %s", re.sub(r"[^\w.@-]", "?", user.username)[:64], request.remote_addr)
            db.session.rollback()
            staffsec.record_failure(user.username, request.remote_addr, "code")
            db.session.commit()
            flash("That code is not right. Codes change every 30 seconds: enter the one showing now.", "error")
            return render_template("auth/second_step.html"), 401
        staffsec.clear_failures(user.username, request.remote_addr)
        _finish_login(user, pending["method"])
        if how == "recovery":
            left = staffsec.recovery_codes_left(user)
            log_event("login.recovery_code", f"{left} left")
            db.session.commit()
            flash(f"You signed in with a recovery code. {left} left: make new ones under My account if you are running out.", "info")
        if user.must_change_password:
            return redirect(url_for("main.account"))
        return redirect(_safe_next(pending.get("next")) or url_for("main.dashboard"))
    return render_template("auth/second_step.html")


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
