"""Integration API (JSON, bearer-token auth) used by connected systems such as the RosarioSIS plugin.

Every request needs ``Authorization: Bearer <token>``; tokens are created by an administrator
under Administration > Integrations. Records from the other system are matched by
``external_id`` (for example ``rosario:student:42``), never by display names.
"""
import hashlib
import re
import secrets
from datetime import timezone
from functools import wraps

from flask import Blueprint, g, jsonify, request, url_for

from .. import __version__
from ..extensions import csrf, db
from ..grading import counted_attempts, enforce_deadlines
from ..models import ApiToken, Course, Enrollment, User, utcnow
from ..utils import get_setting, log_event

bp = Blueprint("api", __name__, url_prefix="/api/v1")
csrf.exempt(bp)

USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{1,120}$")


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token():
    """Returns (token, prefix). The token is shown once and never stored in clear."""
    token = "sch_" + secrets.token_urlsafe(32)
    return token, token[:10]


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@bp.errorhandler(ApiError)
def _api_error(exc):
    db.session.rollback()
    return jsonify({"error": str(exc)}), exc.status


@bp.errorhandler(404)
def _not_found(exc):
    return jsonify({"error": "Not found"}), 404


@bp.errorhandler(405)
def _not_allowed(exc):
    return jsonify({"error": "Method not allowed"}), 405


def token_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return jsonify({"error": "Missing bearer token"}), 401
        token = ApiToken.query.filter_by(token_hash=hash_token(header[7:].strip()), revoked=False).first()
        if token is None:
            return jsonify({"error": "Invalid or revoked token"}), 401
        now = utcnow()
        if token.last_used_at is None or (now - token.last_used_at).total_seconds() > 60:
            token.last_used_at = now
            db.session.commit()
        g.api_token = token
        return view(*args, **kwargs)

    return wrapped


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("Expected a JSON object body")
    return data


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z") if dt else None


def _text(data, key, limit, required=False):
    value = data.get(key)
    if value is None:
        if required:
            raise ApiError(f"'{key}' is required")
        return None
    if not isinstance(value, (str, int, float)):
        raise ApiError(f"'{key}' must be text")
    value = str(value).strip()
    if required and not value:
        raise ApiError(f"'{key}' is required")
    return value[:limit]


def course_json(course):
    return {
        "id": course.id,
        "code": course.code,
        "title": course.title,
        "term": course.term,
        "description": course.description,
        "archived": course.archived,
        "external_id": course.external_id,
        "students": len(course.enrollments),
        "instructors": [u.username for u in course.instructors],
        "url": url_for("courses.detail", course_id=course.id, _external=True),
    }


def _get_course(course_id):
    course = db.session.get(Course, course_id)
    if course is None:
        raise ApiError("Course not found", 404)
    return course


# --------------------------------------------------------------------------- endpoints


@bp.route("/ping")
@token_required
def ping():
    return jsonify({"ok": True, "app": "scholar", "version": __version__, "institution": get_setting("institution_name")})


@bp.route("/courses", methods=["GET"])
@token_required
def list_courses():
    query = Course.query
    if request.args.get("include_archived") not in ("1", "true", "yes"):
        query = query.filter(Course.archived.is_(False))
    if request.args.get("external_id"):
        query = query.filter(Course.external_id == request.args["external_id"])
    return jsonify({"courses": [course_json(c) for c in query.order_by(Course.code, Course.term)]})


@bp.route("/courses", methods=["POST"])
@token_required
def create_course():
    data = _json_body()
    external_id = _text(data, "external_id", 120)
    if external_id:
        existing = Course.query.filter_by(external_id=external_id).first()
        if existing is not None:
            return jsonify({"course": course_json(existing), "created": False}), 200
    course = Course(
        code=_text(data, "code", 40, required=True),
        title=_text(data, "title", 200, required=True),
        term=_text(data, "term", 60) or "",
        description=_text(data, "description", 5000) or "",
        external_id=external_id or None,
        archived=False,
    )
    db.session.add(course)
    log_event("api.course_created", f"{course.code} ({external_id or 'no external id'}) via token {g.api_token.name}")
    db.session.commit()
    return jsonify({"course": course_json(course), "created": True}), 201


@bp.route("/courses/<int:course_id>", methods=["GET"])
@token_required
def get_course(course_id):
    return jsonify({"course": course_json(_get_course(course_id))})


@bp.route("/courses/<int:course_id>", methods=["PATCH"])
@token_required
def update_course(course_id):
    course = _get_course(course_id)
    data = _json_body()
    for key, limit in (("code", 40), ("title", 200), ("term", 60), ("description", 5000)):
        value = _text(data, key, limit)
        if value is not None and (value or key in ("term", "description")):
            setattr(course, key, value)
    if "external_id" in data:
        value = _text(data, "external_id", 120)
        if value:
            clash = Course.query.filter(Course.external_id == value, Course.id != course.id).first()
            if clash is not None:
                raise ApiError(f"Course {clash.id} is already linked to {value}", 409)
        course.external_id = value or None
    if "archived" in data:
        course.archived = bool(data["archived"])
    log_event("api.course_updated", f"{course.code} #{course.id} via token {g.api_token.name}")
    db.session.commit()
    return jsonify({"course": course_json(course)})


def _person(raw, kind):
    if not isinstance(raw, dict):
        raise ApiError(f"Each {kind} must be an object")
    username = _text(raw, "username", 120, required=True)
    if not USERNAME_RE.match(username):
        raise ApiError(f"Invalid username '{username}' (letters, digits and . _ - @ only)")
    return {
        "external_id": _text(raw, "external_id", 120) or None,
        "username": username,
        "full_name": _text(raw, "full_name", 200) or "",
        "email": _text(raw, "email", 200) or "",
        "student_number": _text(raw, "student_number", 64) or "",
    }


def _upsert_user(person, role, account_type, created, conflicts):
    user = None
    if person["external_id"]:
        user = User.query.filter_by(external_id=person["external_id"]).first()
    if user is None:
        user = User.query.filter(db.func.lower(User.username) == person["username"].lower()).first()
        if user is not None and user.external_id and person["external_id"] and user.external_id != person["external_id"]:
            conflicts.append(
                {"username": person["username"], "external_id": person["external_id"], "reason": f"username already belongs to {user.external_id}"}
            )
            return None
    if user is None:
        # Students are test takers with no password (they use the test portal); account_type
        # decides how instructors sign in to the staff site.
        user = User(username=person["username"], role=role, auth_source="portal" if role == "student" else account_type, active=True)
        db.session.add(user)
        db.session.flush()  # assign an id now: callers key users by id
        created.append({"username": user.username, "role": role, "temporary_password": _staff_password(user)})
    if person["external_id"]:
        user.external_id = person["external_id"]
    for field in ("full_name", "email", "student_number"):
        if person[field]:
            setattr(user, field, person[field])
    if role == "instructor" and user.role == "student":
        user.role = "instructor"  # promote, never demote
        user.auth_source = account_type
        temporary = _staff_password(user)
        if temporary:
            created.append({"username": user.username, "role": role, "temporary_password": temporary})
    user.active = True
    return user


def _staff_password(user):
    """A temporary password for a new local staff account, or None."""
    if user.role == "student" or user.auth_source != "local" or user.password_hash:
        return None
    temporary = secrets.token_urlsafe(9)
    user.set_password(temporary)
    user.must_change_password = True
    return temporary


@bp.route("/courses/<int:course_id>/roster", methods=["PUT"])
@token_required
def set_roster(course_id):
    """Make the course roster match the connected system's course period."""
    course = _get_course(course_id)
    data = _json_body()
    account_type = data.get("account_type", "ldap")
    if account_type not in ("ldap", "local"):
        raise ApiError("account_type must be 'ldap' or 'local'")
    students = [_person(p, "student") for p in data.get("students") or []]
    instructors = [_person(p, "instructor") for p in data.get("instructors") or []]
    remove_missing = data.get("remove_missing", True)
    created, conflicts = [], []

    instructor_users = []
    for person in instructors:
        user = _upsert_user(person, "instructor", account_type, created, conflicts)
        if user is not None:
            instructor_users.append(user)
    db.session.flush()
    for user in instructor_users:
        if not course.has_instructor(user):
            course.instructors.append(user)

    wanted = {}
    for person in students:
        user = _upsert_user(person, "student", account_type, created, conflicts)
        if user is not None:
            wanted[user.id] = user
    db.session.flush()
    enrolled, unenrolled = [], []
    current = {e.user_id: e for e in course.enrollments}
    for user_id, user in wanted.items():
        if user_id not in current:
            db.session.add(Enrollment(course=course, user=user))
            enrolled.append(user.username)
    if remove_missing:
        for user_id, enrollment in current.items():
            if user_id not in wanted:
                unenrolled.append(enrollment.user.username)
                db.session.delete(enrollment)
        if data.get("instructors") is not None:
            keep = {u.id for u in instructor_users}
            for user in list(course.instructors):
                # Only instructors that came from a connected system (they carry an external_id)
                # are removed; instructors added by hand in Scholar stay.
                if user.id not in keep and user.external_id:
                    course.instructors.remove(user)
    log_event(
        "api.roster",
        f"{course.code} #{course.id}: +{len(enrolled)} -{len(unenrolled)} students, {len(created)} account(s) created",
    )
    db.session.commit()
    return jsonify(
        {
            "course": course_json(course),
            "enrolled": enrolled,
            "unenrolled": unenrolled,
            "created": created,
            "conflicts": conflicts,
        }
    )


@bp.route("/courses/<int:course_id>/results", methods=["GET"])
@token_required
def course_results(course_id):
    """Every non-draft assessment of the course with each student's counted attempt."""
    course = _get_course(course_id)
    out = []
    for assessment in course.assessments:
        if assessment.status == "draft":
            continue
        enforce_deadlines([a for a in assessment.attempts if a.status == "in_progress"])
        results = []
        for user_id, attempt in counted_attempts(assessment).items():
            user = attempt.user
            results.append(
                {
                    "username": user.username,
                    "external_id": user.external_id,
                    "score": round(attempt.score, 4),
                    "max_score": round(attempt.max_score, 4),
                    "percent": round(attempt.percent, 2),
                    "needs_grading": bool(attempt.needs_grading),
                    "submitted_at": _iso(attempt.submitted_at),
                    "source": attempt.source,
                }
            )
        out.append(
            {
                "id": assessment.id,
                "title": assessment.title,
                "status": assessment.status,
                "window_state": assessment.window_state(),
                "max_score": round(assessment.max_score, 4),
                "opens_at": _iso(assessment.opens_at),
                "closes_at": _iso(assessment.closes_at),
                "created_at": _iso(assessment.created_at),
                "updated_at": _iso(assessment.updated_at),
                "results": sorted(results, key=lambda r: r["username"]),
            }
        )
    return jsonify({"course": course_json(course), "assessments": out})


@bp.route("/users/active", methods=["POST"])
@token_required
def sync_active_users():
    """Activate the listed accounts and deactivate every other account whose external_id has the prefix.

    Used at a new school year to switch off accounts of students who have left. Results are kept.
    """
    data = _json_body()
    prefix = _text(data, "prefix", 60, required=True)
    if len(prefix) < 4 or ":" not in prefix:
        raise ApiError("prefix must look like 'rosario:student:'")
    keep = {str(x) for x in data.get("active_external_ids") or []}
    deactivated = reactivated = 0
    for user in User.query.filter(User.external_id.like(prefix.replace("%", "") + "%")):
        if user.role == "admin":
            continue
        if user.external_id in keep:
            if not user.active:
                user.active = True
                reactivated += 1
        elif user.active:
            user.active = False
            deactivated += 1
    log_event("api.users_active", f"{prefix}: {deactivated} deactivated, {reactivated} reactivated")
    db.session.commit()
    return jsonify({"deactivated": deactivated, "reactivated": reactivated})
