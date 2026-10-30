from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..grading import counted_attempts, enforce_deadlines
from ..models import Assessment, Attempt, Course, Enrollment, course_instructors
from ..utils import log_event

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


def student_assessment_rows(user, courses):
    rows = []
    for course in courses:
        for assessment in course.assessments:
            if assessment.status == "draft":
                continue
            attempts = [a for a in assessment.attempts if a.user_id == user.id]
            enforce_deadlines(attempts)
            in_progress = next((a for a in attempts if a.status == "in_progress"), None)
            submitted = [a for a in attempts if a.status == "submitted"]
            counted = counted_attempts(assessment).get(user.id)
            remaining = None if assessment.max_attempts == 0 else max(0, assessment.max_attempts - len(attempts))
            can_start = (
                assessment.is_open() and in_progress is None and (remaining is None or remaining > 0) and len(assessment.items) > 0
            )
            rows.append(
                {
                    "assessment": assessment,
                    "course": course,
                    "state": assessment.window_state(),
                    "in_progress": in_progress,
                    "submitted": submitted,
                    "counted": counted,
                    "remaining": remaining,
                    "can_start": can_start,
                    "results_visible": assessment.results_visible(),
                }
            )
    order = {"open": 0, "upcoming": 1, "closed": 2}
    rows.sort(key=lambda r: (r["in_progress"] is None, order.get(r["state"], 3), r["assessment"].closes_at or r["assessment"].created_at))
    return rows


@bp.route("/")
@login_required
def dashboard():
    courses = visible_courses(current_user)
    if current_user.is_student:
        rows = student_assessment_rows(current_user, courses)
        return render_template("dashboard_student.html", courses=courses, rows=rows)

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
        elif len(new) < 8:
            flash("The new password must be at least 8 characters long.", "error")
        elif new != confirm:
            flash("The new passwords do not match.", "error")
        elif new == current:
            flash("Please choose a password different from the current one.", "error")
        else:
            current_user.set_password(new)
            current_user.must_change_password = False
            log_event("account.password_changed")
            db.session.commit()
            flash("Password updated.", "success")
            return redirect(url_for("main.dashboard"))
    return render_template("account.html")


@bp.before_app_request
def force_password_change():
    if (
        current_user.is_authenticated
        and current_user.must_change_password
        and current_user.auth_source == "local"
        and request.endpoint not in ("main.account", "auth.logout", "static")
        and not (request.endpoint or "").startswith("api.")
    ):
        return redirect(url_for("main.account"))
