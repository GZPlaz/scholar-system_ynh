import secrets
from zoneinfo import available_timezones

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from .. import syncevents
from ..extensions import db
from ..models import ROLES, AuditLog, Course, Enrollment, SyncEvent, User
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
        user.role = role
        user.auth_source = auth_source
        user.active = parse_bool(form.get("active", ""))
        password = form.get("password", "")
        if role == "student":
            user.password_hash = None
            user.must_change_password = False
        elif auth_source == "local":
            if password:
                if len(password) < 8:
                    db.session.rollback()
                    flash("Passwords must be at least 8 characters.", "error")
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
        )
    }
    return render_template("admin/settings.html", values=values, zones=zones)


@bp.route("/audit")
@admin_required
def audit():
    page = max(1, parse_int(request.args.get("page"), 1))
    pagination = AuditLog.query.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=100, error_out=False)
    return render_template("admin/audit.html", pagination=pagination)


@bp.route("/integrations", methods=["GET", "POST"])
@admin_required
def integrations():
    from ..models import ApiToken
    from .api import hash_token, new_token

    new_secret = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = request.form.get("name", "").strip()[:120]
            if not name:
                flash("Give the token a name, for example “RosarioSIS”.", "error")
            else:
                secret, prefix = new_token()
                db.session.add(ApiToken(name=name, prefix=prefix, token_hash=hash_token(secret), created_by_id=current_user.id))
                log_event("api.token_created", name)
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
