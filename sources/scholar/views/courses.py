import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..grading import counted_attempts
from ..models import Course, Enrollment, User
from ..utils import can_manage_course, log_event, parse_bool, read_csv_upload, require_course_manager, staff_required
from .main import visible_courses

bp = Blueprint("courses", __name__, url_prefix="/courses")


def get_course(course_id):
    course = db.session.get(Course, course_id)
    if course is None:
        abort(404)
    return course


@bp.route("/")
@login_required
def index():
    show_archived = parse_bool(request.args.get("archived", ""))
    courses = visible_courses(current_user, include_archived=show_archived)
    return render_template("courses/index.html", courses=courses, show_archived=show_archived)


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:course_id>/edit", methods=["GET", "POST"])
@staff_required
def edit(course_id=None):
    course = get_course(course_id) if course_id else None
    if course is not None:
        require_course_manager(course)
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        title = request.form.get("title", "").strip()
        if not code or not title:
            flash("Course code and title are required.", "error")
            return render_template("courses/form.html", course=course, form=request.form)
        if course is None:
            course = Course()
            db.session.add(course)
            if not current_user.is_admin:
                course.instructors.append(current_user)
        course.code = code[:40]
        course.title = title[:200]
        course.term = request.form.get("term", "").strip()[:60]
        course.description = request.form.get("description", "").strip()
        course.archived = parse_bool(request.form.get("archived", ""))
        log_event("course.saved", course.code)
        db.session.commit()
        flash("Course saved.", "success")
        return redirect(url_for("courses.detail", course_id=course.id))
    return render_template("courses/form.html", course=course, form={})


@bp.route("/<int:course_id>")
@login_required
def detail(course_id):
    course = get_course(course_id)
    if can_manage_course(course):
        stats = []
        for assessment in course.assessments:
            counted = counted_attempts(assessment)
            percents = [a.percent for a in counted.values()]
            stats.append(
                {
                    "assessment": assessment,
                    "submitted": len(counted),
                    "mean": sum(percents) / len(percents) if percents else None,
                    "to_grade": sum(1 for a in assessment.attempts if a.needs_grading and a.status == "submitted"),
                    "in_progress": sum(1 for a in assessment.attempts if a.status == "in_progress"),
                }
            )
        return render_template("courses/detail.html", course=course, stats=stats)
    if course.has_student(current_user):
        return redirect(url_for("main.dashboard"))
    abort(403)


def _lookup_users(tokens):
    found, missing = [], []
    for token in tokens:
        user = User.query.filter(
            db.or_(db.func.lower(User.username) == token.lower(), User.student_number == token)
        ).first()
        (found.append(user) if user else missing.append(token))
    return found, missing


@bp.route("/<int:course_id>/roster", methods=["GET", "POST"])
@staff_required
def roster(course_id):
    course = get_course(course_id)
    require_course_manager(course)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            tokens = [t for t in re.split(r"[\s,;]+", request.form.get("usernames", "")) if t]
            users, missing = _lookup_users(tokens)
            added = 0
            for user in users:
                if not course.has_student(user):
                    db.session.add(Enrollment(course=course, user=user))
                    added += 1
            log_event("course.enroll", f"{course.code}: +{added}")
            db.session.commit()
            flash(f"{added} student(s) enrolled.", "success")
            if missing:
                flash("Not found: " + ", ".join(missing[:30]) + (" …" if len(missing) > 30 else ""), "error")
        elif action == "import":
            rows = read_csv_upload()
            if rows is not None:
                tokens = [r.get("username") or r.get("student_id") or r.get("student_number") or "" for r in rows]
                users, missing = _lookup_users([t for t in tokens if t])
                added = 0
                for user in users:
                    if not course.has_student(user):
                        db.session.add(Enrollment(course=course, user=user))
                        added += 1
                db.session.commit()
                flash(f"{added} student(s) enrolled from CSV.", "success")
                if missing:
                    flash(f"{len(missing)} row(s) did not match a user: " + ", ".join(missing[:20]), "error")
        elif action == "remove":
            enrollment = Enrollment.query.filter_by(course_id=course.id, user_id=request.form.get("user_id", type=int)).first()
            if enrollment:
                log_event("course.unenroll", f"{course.code}: {enrollment.user.username}")
                db.session.delete(enrollment)
                db.session.commit()
                flash("Student removed from the course (their past results are kept).", "success")
        elif action in ("add_instructor", "remove_instructor"):
            if not current_user.is_admin:
                abort(403)
            user = User.query.filter(db.func.lower(User.username) == request.form.get("username", "").strip().lower()).first()
            if user is None:
                flash("User not found.", "error")
            elif action == "add_instructor":
                if user.role == "student":
                    flash(f"{user.username} is a student. Change their role to instructor first.", "error")
                elif not course.has_instructor(user):
                    course.instructors.append(user)
                    db.session.commit()
                    flash(f"{user.display_name} now teaches {course.code}.", "success")
            else:
                if course.has_instructor(user):
                    course.instructors.remove(user)
                    db.session.commit()
                    flash("Instructor removed.", "success")
        return redirect(url_for("courses.roster", course_id=course.id))
    return render_template("courses/roster.html", course=course)


@bp.route("/<int:course_id>/delete", methods=["POST"])
@staff_required
def delete(course_id):
    course = get_course(course_id)
    require_course_manager(course)
    if any(a.attempts for a in course.assessments):
        course.archived = True
        flash("This course has student results, so it was archived instead of deleted.", "info")
    else:
        db.session.delete(course)
        flash("Course deleted.", "success")
    log_event("course.deleted", course.code)
    db.session.commit()
    return redirect(url_for("courses.index"))
