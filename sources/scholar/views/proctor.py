"""Live proctoring of an assessment: who is in, who is writing, and the proctor's controls."""
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..grading import counted_attempts, enforce_deadlines, finalize_attempt
from ..models import Accommodation, TestSession, User, utcnow
from ..security import (
    accommodation_for,
    add_event,
    generate_code,
    lock_attempt,
    rotating_password,
    unlock_attempt,
    validate_custom_code,
)
from ..taking import remaining_seconds
from ..utils import log_event, parse_int, staff_required
from .assessments import get_assessment

bp = Blueprint("proctor", __name__, url_prefix="/assessments")

MAX_ADDED_MINUTES = 600


def monitor_rows(assessment):
    """One row per enrolled student (plus anyone else with an attempt)."""
    enforce_deadlines([a for a in assessment.attempts if a.status == "in_progress"])
    now = utcnow()
    sessions = {}
    for s in assessment.test_sessions:
        if s.status in ("waiting", "ready") and s.expires_at > now:
            current = sessions.get(s.user_id)
            if current is None or s.last_seen_at > current.last_seen_at:
                sessions[s.user_id] = s
    attempts = {}
    for a in assessment.attempts:
        attempts.setdefault(a.user_id, []).append(a)
    counted = counted_attempts(assessment)
    students = {u.id: u for u in assessment.course.students}
    for a in assessment.attempts:
        students.setdefault(a.user_id, a.user)
    total_items = len(assessment.items)
    accommodations = {a.user_id: a for a in assessment.accommodations}
    rows = []
    for user in sorted(students.values(), key=lambda u: u.sort_key):
        mine = sorted(attempts.get(user.id, []), key=lambda a: a.number)
        live = next((a for a in mine if a.status == "in_progress"), None)
        session = sessions.get(user.id)
        events = live.events if live else []
        if live is not None and live.locked:
            state = "paused"
        elif live is not None:
            state = "writing"
        elif session is not None and session.status == "waiting":
            state = "waiting"
        elif session is not None:
            state = "signed in"
        elif user.id in counted:
            state = "submitted"
        else:
            state = "not started"
        answered = sum(1 for r in live.responses if r.answer is not None) if live else None
        rows.append(
            {
                "user": user,
                "state": state,
                "session": session,
                "live": live,
                "counted": counted.get(user.id),
                "attempts": len(mine),
                "answered": answered,
                "total": total_items,
                "remaining": remaining_seconds(live) if live else None,
                "focus_losses": live.focus_losses if live else 0,
                "device_changes": sum(1 for e in events if e.kind in ("device_change", "device_blocked", "session_hijack_blocked")),
                "ip_changes": sum(1 for e in events if e.kind == "ip_change"),
                "idle": int((now - session.last_seen_at).total_seconds()) if session else None,
                "no_id": not user.student_number,
                "no_pin": assessment.identity_check == "pin" and not user.pin_hash,
                "enrolled": assessment.course.has_student(user),
                "accommodation": accommodations.get(user.id),
                "in_seb": bool(session and session.lockdown_ok and assessment.lockdown_browser == "seb"),
                "position": live.position + 1 if live is not None and assessment.sequential else None,
            }
        )
    return rows


def password_info(assessment):
    """What the proctor reads out in the room, if the assessment asks for a password."""
    if assessment.access_password_mode == "rotating":
        code, seconds = rotating_password(assessment)
        return {"mode": "rotating", "value": f"{code[:3]}-{code[3:]}", "seconds": seconds, "minutes": assessment.password_rotation_minutes}
    if assessment.access_password_mode == "static":
        return {"mode": "static", "value": assessment.access_password}
    return None


def _tally(rows):
    keys = ("not started", "waiting", "signed in", "writing", "paused", "submitted")
    return {k: sum(1 for r in rows if r["state"] == k) for k in keys}


@bp.route("/<int:assessment_id>/monitor")
@staff_required
def monitor(assessment_id):
    assessment = get_assessment(assessment_id)
    rows = monitor_rows(assessment)
    page = {"assessment": assessment, "rows": rows, "tally": _tally(rows), "password": password_info(assessment)}
    if request.args.get("fragment"):
        return render_template("assessments/_monitor_rows.html", **page)
    return render_template("assessments/monitor.html", **page)


@bp.route("/<int:assessment_id>/monitor/code", methods=["POST"])
@staff_required
def change_code(assessment_id):
    assessment = get_assessment(assessment_id)
    custom = request.form.get("code", "").strip()
    if custom:
        code, error = validate_custom_code(custom, assessment)
        if error:
            flash(error, "error")
            return redirect(url_for("proctor.monitor", assessment_id=assessment.id))
    else:
        code = generate_code()
    assessment.access_code = code
    log_event("assessment.code_changed", f"#{assessment.id}")
    db.session.commit()
    flash(f"New test code: {assessment.display_code}. Students already signed in are not affected.", "success")
    return redirect(request.form.get("next") or url_for("proctor.monitor", assessment_id=assessment.id))


@bp.route("/<int:assessment_id>/monitor/admit-all", methods=["POST"])
@staff_required
def admit_all(assessment_id):
    assessment = get_assessment(assessment_id)
    count = 0
    for session in assessment.test_sessions:
        if session.status == "waiting" and session.expires_at > utcnow():
            session.status = "ready"
            count += 1
    log_event("proctor.admit_all", f"#{assessment.id}: {count}")
    db.session.commit()
    flash(f"{count} student(s) let in.", "success")
    return redirect(url_for("proctor.monitor", assessment_id=assessment.id))


@bp.route("/<int:assessment_id>/monitor/student/<int:user_id>/<action>", methods=["POST"])
@staff_required
def student_action(assessment_id, user_id, action):
    assessment = get_assessment(assessment_id)
    user = db.session.get(User, user_id) or abort(404)
    now = utcnow()
    sessions = [
        s for s in TestSession.query.filter_by(assessment_id=assessment.id, user_id=user.id)
        if s.status in ("waiting", "ready") and s.expires_at > now
    ]
    live = next((a for a in assessment.attempts if a.user_id == user.id and a.status == "in_progress"), None)
    who = user.display_name
    if action == "admit":
        for s in sessions:
            if s.status == "waiting":
                s.status = "ready"
        flash(f"{who} was let in.", "success")
    elif action == "reset_device":
        # Sign the student out everywhere, so they can sign in again on another device.
        for s in sessions:
            s.status = "ended"
            s.end_reason = f"signed out by {current_user.username}"
        if live is not None:
            add_event(live, "device_reset", f"by {current_user.username}")
        flash(f"{who} was signed out. They can sign in again on any device.", "success")
    elif action in ("allow_attempt", "extra_time"):
        # accommodations for attempts not started yet
        accommodation = accommodation_for(assessment, user)
        if accommodation is None:
            accommodation = Accommodation(assessment=assessment, user=user)
            db.session.add(accommodation)
        if action == "allow_attempt":
            accommodation.extra_attempts = (accommodation.extra_attempts or 0) + 1
            flash(f"{who} may take the test once more.", "success")
        else:
            minutes = parse_int(request.form.get("minutes"), -1)
            if not 0 <= minutes <= MAX_ADDED_MINUTES:
                flash(f"Extra time must be between 0 and {MAX_ADDED_MINUTES} minutes.", "error")
                return redirect(url_for("proctor.monitor", assessment_id=assessment.id))
            accommodation.extra_minutes = minutes
            flash(f"{who} gets {minutes} extra minute(s) on their next attempt.", "success")
    elif live is None:
        flash(f"{who} has no test in progress.", "error")
        return redirect(url_for("proctor.monitor", assessment_id=assessment.id))
    elif action == "lock":
        lock_attempt(live, "Your proctor paused your test", f"by {current_user.username}")
        flash(f"{who}'s test is paused.", "success")
    elif action == "unlock":
        given = unlock_attempt(live, f"by {current_user.username}")
        flash(f"{who} can continue." + (f" {given} paused minute(s) were given back." if given else ""), "success")
    elif action == "add_time":
        minutes = parse_int(request.form.get("minutes"), 0)
        if not 0 < minutes <= MAX_ADDED_MINUTES:
            flash(f"Add between 1 and {MAX_ADDED_MINUTES} minutes.", "error")
            return redirect(url_for("proctor.monitor", assessment_id=assessment.id))
        live.extra_minutes += minutes
        add_event(live, "time_added", f"{minutes} min by {current_user.username}")
        flash(f"{minutes} minute(s) added for {who}.", "success")
    elif action == "submit":
        finalize_attempt(live)
        add_event(live, "force_submitted", f"by {current_user.username}")
        flash(f"{who}'s test was submitted with their saved answers.", "success")
    else:
        abort(400)
    log_event(f"proctor.{action}", f"assessment #{assessment.id} student {user.username}")
    db.session.commit()
    return redirect(url_for("proctor.monitor", assessment_id=assessment.id))
