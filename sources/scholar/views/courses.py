import re
import secrets

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..extensions import db
from ..grading import counted_attempts
from ..models import Course, Enrollment, User, utcnow
from ..security import normalize_student_id
from ..utils import can_manage_course, log_event, parse_bool, parse_int, read_csv_upload, require_course_manager, staff_required
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
    abort(403)


def find_student_by_id(student_number):
    wanted = normalize_student_id(student_number)
    if not wanted:
        return None
    candidates = User.query.filter(db.func.lower(User.student_number) == student_number.strip().lower()).all()
    candidates = candidates or [u for u in User.query.filter(User.student_number != "") if normalize_student_id(u.student_number) == wanted]
    return candidates[0] if candidates else None


def create_student(full_name, student_number):
    """A test taker: a name and a student ID, no password and no staff-site access."""
    base = "student-" + (re.sub(r"[^a-z0-9._-]", "", student_number.lower()) or secrets.token_hex(3))
    username, n = base[:110], 1
    while User.query.filter(db.func.lower(User.username) == username).first():
        n += 1
        username = f"{base[:105]}-{n}"
    user = User(username=username, full_name=full_name.strip()[:200], student_number=student_number.strip()[:64], role="student", auth_source="portal")
    db.session.add(user)
    db.session.flush()
    return user


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
        elif action == "new_student":
            name = request.form.get("full_name", "").strip()
            number = request.form.get("student_number", "").strip()
            if not name or not normalize_student_id(number):
                flash("Enter the student's name and student ID.", "error")
            else:
                user = find_student_by_id(number)
                if user is not None and not user.is_student:
                    flash(f"The student ID {number} belongs to staff member {user.username}.", "error")
                    return redirect(url_for("courses.roster", course_id=course.id))
                created = user is None
                user = user or create_student(name, number)
                if not course.has_student(user):
                    db.session.add(Enrollment(course=course, user=user))
                log_event("course.enroll", f"{course.code}: {user.username}" + (" (new)" if created else ""))
                db.session.commit()
                flash(f"{user.display_name} ({user.student_number}) is enrolled" + ("." if created else ": that student ID already existed."), "success")
        elif action == "import":
            rows = read_csv_upload()
            if rows is not None:
                added = created = 0
                missing = []
                for row in rows:
                    number = row.get("student_id") or row.get("student_number") or ""
                    token = row.get("username") or number
                    if not token:
                        continue
                    users, _ = _lookup_users([token])
                    user = users[0] if users else None
                    name = row.get("full_name") or row.get("name") or ""
                    if user is None and number and name:
                        user = create_student(name, number)
                        created += 1
                    if user is None:
                        missing.append(token)
                    elif not course.has_student(user):
                        db.session.add(Enrollment(course=course, user=user))
                        added += 1
                log_event("course.import_roster", f"{course.code}: +{added} ({created} new)")
                db.session.commit()
                flash(f"{added} student(s) enrolled from CSV, {created} of them new.", "success")
                if missing:
                    flash(f"{len(missing)} row(s) did not match anyone and had no name to create a student: " + ", ".join(missing[:20]), "error")
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


def new_pin():
    return f"{secrets.randbelow(10**6):06d}"


@bp.route("/<int:course_id>/pins", methods=["POST"])
@staff_required
def pins(course_id):
    """Give students new test-portal PINs and show them once, ready to print and cut into slips."""
    course = get_course(course_id)
    require_course_manager(course)
    scope = request.form.get("scope")
    students = [s for s in course.students if s.is_student]
    if scope == "missing":
        students = [s for s in students if not s.pin_hash]
    elif scope == "one":
        wanted = parse_int(request.form.get("user_id"), 0)
        students = [s for s in students if s.id == wanted]
    elif scope != "all":
        abort(400)
    if not students:
        flash("Every student already has a PIN." if scope == "missing" else "No students to give a PIN to.", "info")
        return redirect(url_for("courses.roster", course_id=course.id))
    slips = []
    for student in students:
        pin = new_pin()
        student.set_pin(pin)
        slips.append({"student": student, "pin": pin})
    log_event("course.pins", f"{course.code}: {len(slips)} PIN(s) ({scope})")
    db.session.commit()
    response = make_response(render_template("courses/pin_slips.html", course=course, slips=slips, made_at=utcnow()))
    response.headers["Cache-Control"] = "no-store"
    return response
