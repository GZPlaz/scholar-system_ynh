"""The test portal (/test/): where students take tests.

Students have no account on the staff site. A student enters the assessment's test code and
their student ID, confirms who they are (and types their PIN when the assessment asks for it),
waits for a proctor if the assessment requires approval, then sits the test. Everything a
browser reports (leaving the page, exiting full screen, blocked copy/paste) is recorded and
judged on the server; the browser is never trusted with a decision.
"""
from datetime import timedelta
from functools import wraps

from flask import Blueprint, abort, g, jsonify, make_response, redirect, render_template, request, url_for

from ..extensions import db
from ..grading import enforce_deadline, finalize_attempt, start_attempt
from ..models import Assessment, Attempt, Enrollment, TestSession, User, utcnow
from ..security import (
    CONFIRM_TTL,
    PORTAL_COOKIE,
    SESSION_TTL,
    add_event,
    clear_pin_failures,
    clear_portal_cookie,
    client_ip,
    hash_token,
    ip_allowed,
    lookup_blocked,
    new_token,
    normalize_code,
    normalize_student_id,
    pin_blocked,
    pin_scope,
    prune_failures,
    record_failure,
    set_portal_cookie,
)
from ..taking import ordered_entries, remaining_seconds, review_entries, store_form_answers
from ..utils import log_event

bp = Blueprint("portal", __name__, url_prefix="/test")

LIVE = ("waiting", "ready")  # statuses of a session that is in use
FOCUS_EVENTS = {"focus_lost", "fullscreen_exit"}
REPORTED_EVENTS = FOCUS_EVENTS | {
    "copy_blocked",
    "paste_blocked",
    "contextmenu_blocked",
    "print_blocked",
    "fullscreen_unsupported",
}
FOCUS_DEDUP = timedelta(seconds=2)  # blur and visibilitychange usually fire together

NOT_FOUND = "We couldn't find a test with that code for that student ID. Check both and try again."


# --------------------------------------------------------------------------- session handling


@bp.app_context_processor
def portal_context():
    test_session = g.get("test_session")
    shown = test_session is not None and test_session.status in LIVE
    return {"signed_in_as": test_session.user.display_name if shown else None, "refresh": g.get("refresh")}


def masked_name(user):
    """First name and last initial: enough for the right student to recognise, little for anyone else."""
    parts = (user.full_name or "").split()
    if not parts:
        return user.username[:2] + "…"
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


def _end(test_session, reason):
    test_session.status = "ended"
    test_session.end_reason = reason[:120]


def load_session(statuses):
    """The portal session from the cookie, if valid and in one of ``statuses``; else None."""
    token = request.cookies.get(PORTAL_COOKIE)
    if not token or len(token) > 100:
        return None
    test_session = TestSession.query.filter_by(token_hash=hash_token(token)).first()
    if test_session is None or test_session.status not in statuses:
        return None
    now = utcnow()
    if test_session.expires_at <= now:
        _end(test_session, "expired")
        db.session.commit()
        return None
    ua = (request.headers.get("User-Agent") or "")[:300]
    if test_session.user_agent and ua != test_session.user_agent:
        # The cookie is being used by another browser: treat it as stolen.
        _end(test_session, "used from another browser")
        if test_session.attempt is not None:
            add_event(test_session.attempt, "session_hijack_blocked", ua[:120])
        db.session.commit()
        return None
    user, assessment = test_session.user, test_session.assessment
    if not user.active or not user.is_student or assessment.status == "draft" or not assessment.course.has_student(user):
        _end(test_session, "no longer allowed")
        db.session.commit()
        return None
    ip = client_ip()
    if ip != test_session.ip:
        if test_session.attempt is not None and test_session.attempt.status == "in_progress":
            add_event(test_session.attempt, "ip_change", f"{test_session.ip} → {ip}")
            test_session.attempt.ip = ip
        test_session.ip = ip
    if (now - test_session.last_seen_at).total_seconds() > 30 or db.session.dirty:
        test_session.last_seen_at = now
        db.session.commit()
    return test_session


def portal_session(*statuses):
    """Require a portal session in one of ``statuses``; otherwise go back to the code page."""

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            test_session = load_session(statuses)
            if test_session is None:
                if request.accept_mimetypes.best == "application/json" or request.path.endswith(("/save", "/event", "/status")):
                    return jsonify({"ok": False, "status": "signed_out", "redirect": url_for("portal.enter")}), 401
                return redirect(url_for("portal.enter"))
            g.test_session = test_session
            return view(*args, **kwargs)

        return wrapped

    return decorator


def message(title, text, code=200, back=True):
    return render_template("portal/message.html", title=title, text=text, back=back), code


def live_sessions(assessment_id, user_id, exclude_id=None):
    now = utcnow()
    query = TestSession.query.filter(
        TestSession.assessment_id == assessment_id,
        TestSession.user_id == user_id,
        TestSession.status.in_(LIVE),
        TestSession.expires_at > now,
    )
    if exclude_id is not None:
        query = query.filter(TestSession.id != exclude_id)
    return query.all()


def current_attempt(assessment, user):
    attempts = [a for a in assessment.attempts if a.user_id == user.id]
    for attempt in attempts:
        enforce_deadline(attempt)
    return attempts, next((a for a in attempts if a.status == "in_progress"), None)


# --------------------------------------------------------------------------- 1. test code and student ID


@bp.route("/", methods=["GET", "POST"])
def enter():
    if request.method == "GET":
        return render_template("portal/enter.html")
    code = normalize_code(request.form.get("code"))
    student_id = normalize_student_id(request.form.get("student_id"))
    if lookup_blocked():
        return message(
            "Too many attempts",
            "Too many test codes or student IDs that didn't match were entered from this network. "
            "Wait 15 minutes, or ask your proctor for help.",
            429,
        )
    if not code or not student_id:
        return render_template("portal/enter.html", error="Enter the test code and your student ID."), 400

    assessment = Assessment.query.filter(Assessment.access_code == code, Assessment.status != "draft").first() if len(code) >= 6 else None
    matches = []
    if assessment is not None:
        rows = (
            db.session.query(User)
            .join(Enrollment, Enrollment.user_id == User.id)
            .filter(Enrollment.course_id == assessment.course_id, User.role == "student", User.active.is_(True), User.student_number != "")
            .all()
        )
        matches = [u for u in rows if normalize_student_id(u.student_number) == student_id]
    if not matches:
        record_failure("lookup")
        prune_failures()
        db.session.commit()
        return render_template("portal/enter.html", error=NOT_FOUND, code=request.form.get("code", "")), 400
    if len(matches) > 1:
        log_event("portal.duplicate_student_id", f"assessment #{assessment.id}: {student_id}")
        db.session.commit()
        return message("Ask your teacher", "Two students in this course share that student ID, so you can't sign in until your teacher fixes it.", 409)
    student = matches[0]
    if not ip_allowed(client_ip(), assessment.allowed_networks):
        log_event("portal.network_refused", f"assessment #{assessment.id} student {student.username}")
        db.session.commit()
        return message("Not from this network", "This test can only be taken from the school's network. Ask your proctor.", 403)

    # A new sign-in on this browser ends any earlier one.
    previous = load_session(("confirm",) + LIVE)
    if previous is not None:
        _end(previous, "signed in again on this browser")
    token, token_hash = new_token()
    now = utcnow()
    db.session.add(
        TestSession(
            token_hash=token_hash,
            assessment=assessment,
            user=student,
            status="confirm",
            ip=client_ip(),
            user_agent=(request.headers.get("User-Agent") or "")[:300],
            created_at=now,
            last_seen_at=now,
            expires_at=now + CONFIRM_TTL,
        )
    )
    db.session.commit()
    return set_portal_cookie(redirect(url_for("portal.verify")), token)


# --------------------------------------------------------------------------- 2. identity


@bp.route("/verify", methods=["GET", "POST"])
@portal_session("confirm")
def verify():
    test_session = g.test_session
    assessment, student = test_session.assessment, test_session.user
    pin_needed = assessment.identity_check == "pin"
    context = {"assessment": assessment, "name": masked_name(student), "pin_needed": pin_needed}
    if request.method == "GET":
        return render_template("portal/verify.html", **context)

    if request.form.get("answer") != "yes":
        _end(test_session, "student said it was not them")
        db.session.commit()
        return clear_portal_cookie(redirect(url_for("portal.enter")))

    if pin_needed:
        if not student.pin_hash:
            return render_template("portal/verify.html", error="You don't have a PIN yet. Ask your teacher for one.", **context), 403
        if pin_blocked(assessment.id, student.id):
            _end(test_session, "too many wrong PINs")
            db.session.commit()
            return message("Too many wrong PINs", "Wait 15 minutes, or ask your proctor for help.", 429)
        if not student.check_pin("".join((request.form.get("pin") or "").split())[:32]):
            record_failure(pin_scope(assessment.id, student.id))
            test_session.pin_failures += 1
            db.session.commit()
            return render_template("portal/verify.html", error="That PIN is not right.", **context), 401
        clear_pin_failures(assessment.id, student.id)

    others = live_sessions(assessment.id, student.id, exclude_id=test_session.id)
    attempts, in_progress = current_attempt(assessment, student)
    if others and in_progress is not None and assessment.lock_to_device:
        add_event(in_progress, "device_blocked", (request.headers.get("User-Agent") or "")[:120])
        _end(test_session, "test already open on another device")
        db.session.commit()
        return message(
            "Open on another device",
            "Your test is already open on another device or browser. Ask your proctor to move it to this one.",
            409,
        )
    for other in others:
        _end(other, "replaced by a sign-in on another device")
    if others and in_progress is not None:
        add_event(in_progress, "device_change", (request.headers.get("User-Agent") or "")[:120])

    # Identity confirmed: issue a fresh token so the pre-verification one is worthless.
    token, token_hash = new_token()
    test_session.token_hash = token_hash
    test_session.status = "waiting" if assessment.require_approval else "ready"
    test_session.expires_at = utcnow() + SESSION_TTL
    test_session.attempt = in_progress
    db.session.commit()
    return set_portal_cookie(redirect(url_for("portal.lobby")), token)


# --------------------------------------------------------------------------- 3. lobby (start / resume / results)


def start_problem(assessment, attempts, in_progress):
    if in_progress is not None:
        return None
    if not assessment.allow_online:
        return "This test is taken on paper."
    state = assessment.window_state()
    if state == "upcoming":
        return "This test has not opened yet."
    if state != "open":
        return "This test is closed."
    if not assessment.items:
        return "This test has no questions yet."
    if assessment.max_attempts and len(attempts) >= assessment.max_attempts:
        return "You have used all of your attempts."
    cutoff = assessment.late_start_cutoff()
    if cutoff and utcnow() > cutoff:
        return "It is too late to start this test. Ask your proctor."
    return None


@bp.route("/start", methods=["GET", "POST"])
@portal_session(*LIVE)
def lobby():
    test_session = g.test_session
    assessment, student = test_session.assessment, test_session.user
    attempts, in_progress = current_attempt(assessment, student)
    db.session.commit()
    if test_session.status == "waiting":
        g.refresh = 5  # moves on by itself once a proctor lets the student in
        return render_template("portal/lobby.html", assessment=assessment, student=student, waiting=True, attempts=attempts)
    problem = start_problem(assessment, attempts, in_progress)
    if request.method == "POST":
        if in_progress is None:
            if problem:
                return redirect(url_for("portal.lobby"))
            in_progress = start_attempt(assessment, student)
            in_progress.ip = client_ip()
            db.session.flush()
            add_event(in_progress, "started")
            log_event("attempt.started", f"assessment #{assessment.id} student {student.username} (test portal)")
        else:
            add_event(in_progress, "resumed")
        test_session.attempt = in_progress
        db.session.commit()
        return redirect(url_for("portal.take"))
    return render_template(
        "portal/lobby.html",
        assessment=assessment,
        student=student,
        waiting=False,
        attempts=attempts,
        in_progress=in_progress,
        problem=problem,
    )


# --------------------------------------------------------------------------- 4. the test


def _session_attempt(test_session):
    """The in-progress attempt this session may work on, or None."""
    attempt = test_session.attempt
    if attempt is None or attempt.user_id != test_session.user_id or attempt.assessment_id != test_session.assessment_id:
        return None
    return attempt


def _state(attempt):
    return {"remaining": remaining_seconds(attempt), "locked": bool(attempt.locked), "lock_reason": attempt.lock_reason}


@bp.route("/attempt")
@portal_session("ready")
def take():
    attempt = _session_attempt(g.test_session)
    if attempt is None:
        return redirect(url_for("portal.lobby"))
    if enforce_deadline(attempt):
        db.session.commit()
    if attempt.status != "in_progress":
        return redirect(url_for("portal.review", attempt_id=attempt.id))
    if attempt.locked:
        g.refresh = 5
        return render_template("portal/locked.html", assessment=attempt.assessment, attempt=attempt)
    return render_template(
        "portal/take.html",
        assessment=attempt.assessment,
        attempt=attempt,
        student=attempt.user,
        entries=ordered_entries(attempt),
        preview=False,
        remaining=remaining_seconds(attempt),
    )


def _closed_response(attempt):
    return jsonify({"ok": False, "status": "closed", "redirect": url_for("portal.review", attempt_id=attempt.id)}), 409


@bp.route("/attempt/save", methods=["POST"])
@portal_session("ready")
def autosave():
    attempt = _session_attempt(g.test_session)
    if attempt is None:
        return jsonify({"ok": False, "status": "signed_out", "redirect": url_for("portal.lobby")}), 401
    if enforce_deadline(attempt) or attempt.status != "in_progress":
        db.session.commit()
        return _closed_response(attempt)
    if attempt.locked:
        db.session.commit()
        return jsonify({"ok": False, "status": "locked", **_state(attempt)}), 423
    store_form_answers(attempt, request.form)
    db.session.commit()
    return jsonify({"ok": True, **_state(attempt)})


@bp.route("/attempt/status")
@portal_session("ready")
def status():
    attempt = _session_attempt(g.test_session)
    if attempt is None:
        return jsonify({"ok": False, "status": "signed_out", "redirect": url_for("portal.lobby")}), 401
    if enforce_deadline(attempt) or attempt.status != "in_progress":
        db.session.commit()
        return _closed_response(attempt)
    return jsonify({"ok": True, "status": "locked" if attempt.locked else "in_progress", **_state(attempt)})


@bp.route("/attempt/event", methods=["POST"])
@portal_session("ready")
def event():
    attempt = _session_attempt(g.test_session)
    if attempt is None or attempt.status != "in_progress":
        return jsonify({"ok": False}), 409
    kind = (request.form.get("kind") or "").strip()
    if kind not in REPORTED_EVENTS:
        abort(400)
    assessment = attempt.assessment
    if kind in FOCUS_EVENTS:
        last = next((e for e in reversed(attempt.events) if e.kind in FOCUS_EVENTS), None)
        if last is None or utcnow() - last.created_at > FOCUS_DEDUP:
            attempt.focus_losses += 1
            add_event(attempt, kind, f"#{attempt.focus_losses}")
            limit = assessment.max_focus_losses
            # each unlock by a proctor gives the student a fresh allowance; the total is kept
            since_unlock = 0
            for e in reversed(attempt.events):
                if e.kind == "unlocked":
                    break
                if e.kind in FOCUS_EVENTS:
                    since_unlock += 1
            if limit and since_unlock >= limit and not attempt.locked:
                attempt.locked = True
                attempt.lock_reason = f"You left the test {since_unlock} time{'s' if since_unlock != 1 else ''}"
                add_event(attempt, "locked", attempt.lock_reason)
    else:
        add_event(attempt, kind)
    db.session.commit()
    return jsonify({"ok": True, **_state(attempt)})


@bp.route("/attempt/submit", methods=["POST"])
@portal_session("ready")
def submit():
    attempt = _session_attempt(g.test_session)
    if attempt is None:
        return redirect(url_for("portal.lobby"))
    if attempt.status == "in_progress":
        if enforce_deadline(attempt):
            pass
        elif attempt.locked:
            db.session.commit()
            return redirect(url_for("portal.take"))
        else:
            store_form_answers(attempt, request.form)
            finalize_attempt(attempt)
            add_event(attempt, "submitted")
            log_event("attempt.submitted", f"assessment #{attempt.assessment_id} attempt #{attempt.id} (test portal)")
        db.session.commit()
    return redirect(url_for("portal.review", attempt_id=attempt.id))


# --------------------------------------------------------------------------- 5. after the test


@bp.route("/results/<int:attempt_id>")
@portal_session("ready")
def review(attempt_id):
    test_session = g.test_session
    attempt = db.session.get(Attempt, attempt_id)
    if attempt is None or attempt.user_id != test_session.user_id or attempt.assessment_id != test_session.assessment_id:
        abort(404)
    if attempt.status == "in_progress":
        return redirect(url_for("portal.take"))
    visible = attempt.assessment.results_visible()
    return render_template(
        "portal/review.html",
        attempt=attempt,
        assessment=attempt.assessment,
        visible=visible,
        entries=review_entries(attempt) if visible else [],
    )


@bp.route("/leave", methods=["POST"])
def leave():
    test_session = load_session(("confirm",) + LIVE)
    if test_session is not None:
        _end(test_session, "signed out")
        db.session.commit()
    response = make_response(redirect(url_for("portal.enter")))
    return clear_portal_cookie(response)
