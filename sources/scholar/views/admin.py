import secrets
from datetime import timedelta
from zoneinfo import available_timezones

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .. import staffsec, syncevents
from ..extensions import db
from ..models import ROLES, ApiToken, AuditLog, Course, Enrollment, LoginFailure, SyncEvent, User, utcnow
from ..utils import (
    admin_required,
    csv_response,
    get_setting,
    log_event,
    parse_bool,
    parse_float,
    parse_int,
    read_csv_upload,
    set_setting,
)

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _find_user(username):
    return User.query.filter(db.func.lower(User.username) == username.strip().lower()).first()


def _valid_username(username):
    return bool(username) and len(username) <= 120 and all(c.isalnum() or c in "._-@" for c in username)


@bp.route("/users")
@admin_required
def users():
    q = request.args.get("q", "").strip()
    role = request.args.get("role", "")
    query = User.query
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(
            db.or_(
                db.func.lower(User.username).like(like),
                db.func.lower(User.full_name).like(like),
                db.func.lower(User.email).like(like),
                db.func.lower(User.student_number).like(like),
            )
        )
    if role in ROLES:
        query = query.filter(User.role == role)
    page = max(1, parse_int(request.args.get("page"), 1))
    pagination = query.order_by(User.role, User.username).paginate(page=page, per_page=50, error_out=False)
    counts = dict(db.session.query(User.role, db.func.count(User.id)).group_by(User.role).all())
    return render_template("admin/users.html", pagination=pagination, q=q, role=role, counts=counts)


@bp.route("/users/new", methods=["GET", "POST"])
@bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id=None):
    user = db.session.get(User, user_id) if user_id else None
    if user_id and user is None:
        abort(404)
    temp_password = None
    if request.method == "POST":
        form = request.form
        username = form.get("username", "").strip()
        role = form.get("role", "student")
        auth_source = form.get("auth_source", "local")
        if role not in ROLES or (role != "student" and auth_source not in ("local", "ldap")):
            abort(400)
        if role == "student":
            auth_source = "portal"  # test takers: no password, no staff-site sign-in
        if user is None:
            if not _valid_username(username):
                flash("Usernames may contain letters, digits and . _ - @ only.", "error")
                return render_template("admin/user_form.html", user=None, form=form)
            if _find_user(username):
                flash(f"The username '{username}' is already taken.", "error")
                return render_template("admin/user_form.html", user=None, form=form)
            user = User(username=username)
            db.session.add(user)
        if user.id == current_user.id and (role != "admin" or not parse_bool(form.get("active", "1"))):
            flash("You cannot remove your own administrator access or deactivate yourself.", "error")
            return redirect(url_for("admin.edit_user", user_id=user.id))
        user.full_name = form.get("full_name", "").strip()
        user.email = form.get("email", "").strip()
        user.student_number = form.get("student_number", "").strip()
        was = (user.role, user.auth_source, bool(user.active))
        user.role = role
        user.auth_source = auth_source
        user.active = parse_bool(form.get("active", ""))
        if user_id and user.id != current_user.id and (was != (role, auth_source, bool(user.active)) or form.get("password")):
            staffsec.sign_out_everywhere(user)  # a changed role, sign-in method, status or password ends open sessions
        if role == "student" and user.totp_enabled:
            staffsec.turn_off_two_step(user)
        password = form.get("password", "")
        if role == "student":
            user.password_hash = None
            user.must_change_password = False
        elif auth_source == "local":
            if password:
                if staffsec.password_problem(password, user):
                    db.session.rollback()
                    flash(staffsec.password_problem(password, user).replace("The new password", "The password"), "error")
                    return render_template("admin/user_form.html", user=user if user_id else None, form=form)
                user.set_password(password)
                user.must_change_password = parse_bool(form.get("must_change_password", ""))
            elif not user.password_hash:
                temp_password = secrets.token_urlsafe(9)
                user.set_password(temp_password)
                user.must_change_password = True
        log_event("user.saved", f"{user.username} role={role} source={auth_source}")
        db.session.commit()
        if temp_password:
            flash(f"Account created. Temporary password for {user.username}: {temp_password}", "success")
        else:
            flash("User saved.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", user=user, form={})


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def reset_password(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.auth_source != "local" or user.is_student:
        flash("This account authenticates through YunoHost; reset its password there.", "error")
        return redirect(url_for("admin.users"))
    password = secrets.token_urlsafe(9)
    user.set_password(password)
    user.must_change_password = True
    staffsec.sign_out_everywhere(user)
    log_event("user.password_reset", user.username)
    db.session.commit()
    flash(f"New temporary password for {user.username}: {password}", "success")
    return redirect(url_for("admin.users", q=user.username))


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.users"))
    if user.attempts:
        user.active = False
        staffsec.sign_out_everywhere(user)
        log_event("user.deactivated", user.username)
        flash(f"{user.username} has assessment history, so the account was deactivated instead of deleted.", "info")
    else:
        log_event("user.deleted", user.username)
        db.session.delete(user)
        flash("User deleted.", "success")
    db.session.commit()
    return redirect(url_for("admin.users"))


@bp.route("/users/import", methods=["GET", "POST"])
@admin_required
def import_users():
    created_passwords = []
    if request.method == "POST":
        rows = read_csv_upload()
        if rows is None:
            return redirect(url_for("admin.import_users"))
        created = updated = skipped = 0
        errors = []
        for lineno, row in enumerate(rows, start=2):
            username = row.get("username", "")
            if not _valid_username(username):
                errors.append(f"Line {lineno}: invalid or missing username")
                skipped += 1
                continue
            role = (row.get("role") or "student").lower()
            if role not in ROLES:
                errors.append(f"Line {lineno}: unknown role '{role}'")
                skipped += 1
                continue
            source = (row.get("auth") or row.get("auth_source") or "local").lower()
            source = "ldap" if source in ("ldap", "yunohost", "sso") else "local"
            if role == "student":
                source = "portal"  # students never get a password
            user = _find_user(username)
            if user is None:
                user = User(username=username, role=role, auth_source=source)
                db.session.add(user)
                created += 1
                if source == "local":
                    password = row.get("password") or secrets.token_urlsafe(9)
                    user.set_password(password)
                    user.must_change_password = True
                    if not row.get("password"):
                        created_passwords.append((username, password))
            else:
                updated += 1
                if row.get("role"):
                    user.role = role
            for field, key in (("full_name", "full_name"), ("full_name", "name"), ("email", "email"), ("student_number", "student_id"), ("student_number", "student_number")):
                if row.get(key):
                    setattr(user, field, row[key])
            course_code = row.get("course") or row.get("course_code")
            if course_code:
                db.session.flush()
                course = Course.query.filter(db.func.lower(Course.code) == course_code.lower(), Course.archived.is_(False)).first()
                if course is None:
                    errors.append(f"Line {lineno}: course '{course_code}' not found (user still imported)")
                elif user.role == "student":
                    if not Enrollment.query.filter_by(course_id=course.id, user_id=user.id).first():
                        db.session.add(Enrollment(course=course, user=user))
                elif not course.has_instructor(user):
                    course.instructors.append(user)
        log_event("user.import", f"created={created} updated={updated} skipped={skipped}")
        db.session.commit()
        flash(f"Import finished: {created} created, {updated} updated, {skipped} skipped.", "success")
        for err in errors[:20]:
            flash(err, "error")
        if created_passwords:
            return csv_response("scholar-new-passwords.csv", ["username", "temporary_password"], created_passwords)
        return redirect(url_for("admin.users"))
    return render_template("admin/import_users.html")


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    zones = sorted(available_timezones())
    if request.method == "POST":
        form = request.form
        mastery = parse_float(form.get("mastery_threshold"), 80)
        approaching = parse_float(form.get("approaching_threshold"), 60)
        if not (0 < approaching < mastery <= 100):
            flash("Thresholds must satisfy 0 < approaching < mastery ≤ 100.", "error")
            return redirect(url_for("admin.settings"))
        tz = form.get("timezone", "UTC")
        if tz not in zones:
            tz = "UTC"
        set_setting("institution_name", form.get("institution_name", "").strip() or "MindAccel University")
        set_setting("timezone", tz)
        set_setting("mastery_threshold", f"{mastery:g}")
        set_setting("approaching_threshold", f"{approaching:g}")
        set_setting("ldap_autoprovision", "1" if parse_bool(form.get("ldap_autoprovision", "")) else "0")
        set_setting("ldap_default_role", "instructor")
        set_setting("attempt_policy", "latest" if form.get("attempt_policy") == "latest" else "best")
        set_setting("login_message", form.get("login_message", "").strip()[:1000])
        scale, problem = parse_grade_scale(form.get("grade_scale", ""))
        if problem:
            flash(problem, "error")
            return redirect(url_for("admin.settings"))
        set_setting("grade_scale", "\n".join(f"{letter} {low:g}" for letter, low in scale))
        log_event("settings.saved")
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("admin.settings"))
    values = {
        key: get_setting(key)
        for key in (
            "institution_name",
            "timezone",
            "mastery_threshold",
            "approaching_threshold",
            "ldap_autoprovision",
            "ldap_default_role",
            "attempt_policy",
            "login_message",
            "grade_scale",
        )
    }
    return render_template("admin/settings.html", values=values, zones=zones)


@bp.route("/security", methods=["GET", "POST"])
@admin_required
def security():
    """Who can get in and how: two-step sign-in, sign-in failures, sessions and API tokens at a glance."""
    if request.method == "POST":
        action = request.form.get("action")
        target = db.session.get(User, parse_int(request.form.get("user_id"), 0))
        if action == "save":
            idle = max(0, min(parse_int(request.form.get("idle_timeout_minutes"), 120) or 0, 24 * 60))
            set_setting("idle_timeout_minutes", str(idle))
            set_setting("require_two_step", "1" if parse_bool(request.form.get("require_two_step", "")) else "0")
            log_event("security.settings_saved", f"two-step required: {get_setting('require_two_step')}, idle sign-out: {idle} min")
            flash("Security settings saved.", "success")
        elif action == "reset_two_step" and target is not None:
            staffsec.turn_off_two_step(target)
            staffsec.sign_out_everywhere(target)
            log_event("security.two_step_reset", target.username)
            flash(f"Two-step sign-in was reset for {target.username}. They are signed out and can set it up again.", "success")
        elif action == "sign_out" and target is not None:
            staffsec.sign_out_everywhere(target)
            log_event("security.signed_out", target.username)
            flash(f"{target.username} is signed out everywhere.", "success")
        elif action == "sign_out_all":
            for user in User.query.filter(User.role.in_(("admin", "instructor")), User.id != current_user.id):
                staffsec.sign_out_everywhere(user)
            log_event("security.signed_out_all")
            flash("Everyone except you is signed out of the staff site.", "success")
        elif action == "unlock":
            name = request.form.get("username", "").lower()[:120]
            LoginFailure.query.filter_by(username=name).delete(synchronize_session=False)
            log_event("security.unlocked", name)
            flash(f"Sign-in attempts for {name} were cleared.", "success")
        db.session.commit()
        return redirect(url_for("admin.security"))
    staff = sorted(User.query.filter(User.role.in_(("admin", "instructor")), User.active.is_(True)), key=lambda u: (u.role != "admin", u.sort_key))
    since = utcnow() - staffsec.WINDOW
    recent = LoginFailure.query.order_by(LoginFailure.created_at.desc()).limit(25).all()
    counts = (
        db.session.query(LoginFailure.username, db.func.count(LoginFailure.id))
        .filter(LoginFailure.created_at >= since, LoginFailure.username != "")
        .group_by(LoginFailure.username)
        .all()
    )
    held = sorted(name for name, n in counts if n >= staffsec.PER_ACCOUNT_AND_IP)
    day = utcnow() - timedelta(days=1)
    tokens = ApiToken.query.filter_by(revoked=False).all()
    return render_template(
        "admin/security.html",
        staff=staff,
        without_two_step=[u for u in staff if not u.totp_enabled],
        recent=recent,
        held=held,
        failures_today=LoginFailure.query.filter(LoginFailure.created_at >= day).count(),
        tokens=tokens,
        now=utcnow(),
        values={k: get_setting(k) for k in ("require_two_step", "idle_timeout_minutes")},
        secure_cookies=current_app.config["SESSION_COOKIE_SECURE"],
        default_key=current_app.config["SECRET_KEY"] == "dev-insecure-change-me",
        https=request.is_secure,
    )


def parse_grade_scale(text):
    """Letter grades as [(letter, lowest percent), …] from lines like "A 90". Returns (scale, problem)."""
    scale = []
    for line in (text or "").splitlines():
        parts = line.replace(",", " ").replace("%", " ").split()
        if not parts:
            continue
        low = parse_float(parts[-1]) if len(parts) >= 2 else None
        if low is None or not 0 <= low <= 100 or len(parts[0]) > 6:
            return [], f"Grade scale: “{line.strip()}” should be a letter and its lowest percentage, for example “A 90”."
        scale.append((parts[0], low))
    scale.sort(key=lambda pair: -pair[1])
    if len({low for _, low in scale}) != len(scale):
        return [], "Grade scale: two letters start at the same percentage."
    return scale, None


@bp.route("/audit")
@admin_required
def audit():
    page = max(1, parse_int(request.args.get("page"), 1))
    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=100, error_out=False)
    return render_template("admin/audit.html", pagination=pagination)


@bp.route("/integrations", methods=["GET", "POST"])
@admin_required
def integrations():
    from ..security import parse_networks
    from .api import hash_token, new_token

    new_secret = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = request.form.get("name", "").strip()[:120]
            if not name:
                flash("Give the token a name, for example “RosarioSIS”.", "error")
            else:
                days = parse_int(request.form.get("expires_days"), 0) or 0
                networks, problems = parse_networks(request.form.get("allowed_networks", ""))
                if problems:
                    flash(problems[0], "error")
                    return redirect(url_for("admin.integrations"))
                secret, prefix = new_token()
                token = ApiToken(
                    name=name,
                    prefix=prefix,
                    token_hash=hash_token(secret),
                    created_by_id=current_user.id,
                    expires_at=utcnow() + timedelta(days=min(days, 3650)) if days > 0 else None,
                    read_only=parse_bool(request.form.get("read_only", "")),
                    allowed_networks="\n".join(str(n) for n in networks),
                )
                db.session.add(token)
                log_event("api.token_created", f"{name}{' (read-only)' if token.read_only else ''}{f', expires in {days} days' if days > 0 else ''}")
                db.session.commit()
                new_secret = secret
        elif action == "revoke":
            token = db.session.get(ApiToken, parse_int(request.form.get("token_id"), 0))
            if token is not None and not token.revoked:
                token.revoked = True
                log_event("api.token_revoked", token.name)
                db.session.commit()
                flash(f"Token “{token.name}” revoked. Anything using it stops working immediately.", "success")
            return redirect(url_for("admin.integrations"))
        elif action in ("webhook_save", "webhook_test", "webhook_clear", "webhook_rotate"):
            return _webhook_action(action)
    tokens = ApiToken.query.order_by(ApiToken.revoked, ApiToken.created_at.desc()).all()
    api_url = url_for("api.ping", _external=True).rsplit("/ping", 1)[0]
    hook_url, hook_secret = syncevents.webhook()
    events = SyncEvent.query.order_by(SyncEvent.id.desc()).limit(15).all()
    return render_template(
        "admin/integrations.html",
        tokens=tokens,
        new_secret=new_secret,
        api_url=api_url,
        hook_url=hook_url,
        hook_secret=hook_secret,
        hook=syncevents.stats(),
        events=events,
        now=utcnow(),
        max_attempts=syncevents.MAX_ATTEMPTS,
    )


def _webhook_action(action):
    if action == "webhook_test":
        ok, message = syncevents.send_test()
        flash(("The other system answered the test: " if ok else "The test did not get through: ") + message, "success" if ok else "error")
    elif action == "webhook_clear":
        syncevents.clear_webhook()
        log_event("admin.webhook_removed")
        db.session.commit()
        flash("Instant updates are off. The other system still gets the changes if it asks for them.", "success")
    else:
        try:
            syncevents.save_webhook(request.form.get("url", ""), rotate=action == "webhook_rotate")
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            log_event("admin.webhook_saved", syncevents.webhook()[0])
            db.session.commit()
            flash("Saved. Scholar tells that address as soon as grades change." if action == "webhook_save" else "New signing secret created: copy it into the other system.", "success")
    return redirect(url_for("admin.integrations"))
