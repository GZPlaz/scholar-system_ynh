from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required

from .. import staffsec
from ..extensions import db
from ..models import Assessment, Attempt, Course, Enrollment, course_instructors
from ..utils import get_setting, log_event, parse_bool

bp = Blueprint("main", __name__)


def visible_courses(user, include_archived=False):
    if user.is_admin:
        query = Course.query
    elif user.is_staff:
        query = Course.query.join(course_instructors).filter(course_instructors.c.user_id == user.id)
    else:
        query = Course.query.join(Enrollment).filter(Enrollment.user_id == user.id)
    if not include_archived:
        query = query.filter(Course.archived.is_(False))
    return query.order_by(Course.code).all()


@bp.route("/")
@login_required
def dashboard():
    courses = visible_courses(current_user)

    course_ids = [c.id for c in courses]
    assessments = (
        Assessment.query.filter(Assessment.course_id.in_(course_ids)).order_by(Assessment.updated_at.desc()).limit(10).all()
        if course_ids
        else []
    )
    to_grade = (
        Attempt.query.join(Assessment)
        .filter(Assessment.course_id.in_(course_ids), Attempt.needs_grading.is_(True), Attempt.status == "submitted")
        .count()
        if course_ids
        else 0
    )
    open_count = sum(1 for a in assessments if a.window_state() == "open")
    student_count = (
        db.session.query(db.func.count(db.distinct(Enrollment.user_id))).filter(Enrollment.course_id.in_(course_ids)).scalar()
        if course_ids
        else 0
    )
    return render_template(
        "dashboard_staff.html",
        courses=courses,
        assessments=assessments,
        to_grade=to_grade,
        open_count=open_count,
        student_count=student_count,
    )


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        if current_user.auth_source != "local":
            flash("Your password is managed by YunoHost. Change it from the YunoHost user portal.", "error")
            return redirect(url_for("main.account"))
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not current_user.check_password(current):
            flash("Your current password is incorrect.", "error")
        elif staffsec.password_problem(new, current_user):
            flash(staffsec.password_problem(new, current_user), "error")
        elif new != confirm:
            flash("The new passwords do not match.", "error")
        elif new == current:
            flash("Please choose a password different from the current one.", "error")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            # Every other browser signed in with the old password is signed out; this one stays.
            staffsec.sign_out_everywhere(current_user)
            session["sv"] = current_user.session_version
            log_event("account.password_changed")
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("account.html")


@bp.route("/account/two-step", methods=["GET", "POST"])
@login_required
def two_step():
    """Set up, renew or turn off two-step sign-in (an authenticator app plus recovery codes)."""
    user = current_user
    new_codes = None
    if request.method == "POST":
        action = request.form.get("action")
        code = request.form.get("code", "")
        if action == "start" and not user.totp_enabled:
            session["totp_setup"] = staffsec.new_secret()
        elif action == "cancel":
            session.pop("totp_setup", None)
        elif action == "confirm" and not user.totp_enabled and session.get("totp_setup"):
            step = staffsec.matching_step(session["totp_setup"], code)
            if step is None:
                flash("That code doesn't match. Check the time on your phone is right, and type the code showing now.", "error")
            else:
                user.totp_secret = session.pop("totp_setup")
                user.totp_enabled = True
                user.totp_last_step = step
                new_codes = staffsec.new_recovery_codes(user)
                staffsec.sign_out_everywhere(user)
                session["sv"] = user.session_version
                log_event("account.two_step_on")
                db.session.commit()
                flash("Two-step sign-in is on. Save the recovery codes below: they are shown only once.", "success")
        elif action in ("codes", "off") and user.totp_enabled:
            # Changing it takes a current code, so an unattended browser can't switch it off.
            if staffsec.throttled(user.username, request.remote_addr):
                flash("Too many wrong codes. Wait a few minutes and try again.", "error")
            elif staffsec.check_second_step(user, code) is None:
                db.session.rollback()
                staffsec.record_failure(user.username, request.remote_addr, "code")
                db.session.commit()
                flash("Enter the current code from your authenticator app (or a recovery code) to do that.", "error")
            elif action == "codes":
                new_codes = staffsec.new_recovery_codes(user)
                log_event("account.recovery_codes_renewed")
                db.session.commit()
                flash("New recovery codes made. The old ones no longer work.", "success")
            elif parse_bool(get_setting("require_two_step")) and session.get("auth_method") != "sso":
                db.session.rollback()
                flash("Two-step sign-in is required for staff accounts and can't be turned off.", "error")
            else:
                staffsec.turn_off_two_step(user)
                staffsec.sign_out_everywhere(user)
                session["sv"] = user.session_version
                log_event("account.two_step_off")
                db.session.commit()
                flash("Two-step sign-in is off.", "success")
        if new_codes is None:
            return redirect(url_for("main.two_step"))
    secret = session.get("totp_setup") if not user.totp_enabled else None
    uri = staffsec.provisioning_uri(secret, user.username) if secret else ""
    return render_template(
        "account_two_step.html",
        secret=staffsec.spaced(secret) if secret else "",
        qr=staffsec.qr_data_uri(uri) if secret else "",
        new_codes=new_codes,
        codes_left=staffsec.recovery_codes_left(user),
        required=parse_bool(get_setting("require_two_step")),
    )


@bp.before_app_request
def force_password_change():
    if (
        current_user.is_authenticated
        and current_user.must_change_password
        and current_user.auth_source == "local"
        and request.endpoint not in ("main.account", "main.two_step", "auth.logout", "static")
        and not (request.endpoint or "").startswith("api.")
    ):
        return redirect(url_for("main.account"))
