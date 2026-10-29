"""Student-facing pages: taking an assessment online and reviewing results."""
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ..analytics import student_mastery
from ..extensions import db
from ..grading import effective_deadline, enforce_deadline, finalize_attempt, save_answer, start_attempt
from ..models import Assessment, Attempt, utcnow
from ..utils import can_manage_course, log_event
from .main import student_assessment_rows, visible_courses

bp = Blueprint("student", __name__)

MAX_TEXT_ANSWER = 20000


def ordered_entries(attempt):
    """Items and choices in the order this attempt presents them."""
    items = {item.id: item for item in attempt.assessment.items}
    order = [i for i in attempt.get_item_order() if i in items]
    order += [i.id for i in attempt.assessment.items if i.id not in order]
    choice_order = attempt.get_choice_order()
    rmap = attempt.response_map()
    entries = []
    for number, item_id in enumerate(order, start=1):
        item = items[item_id]
        choices = list(item.question.choices)
        if item_id in choice_order:
            by_id = {c.id: c for c in choices}
            ordered = [by_id[c] for c in choice_order[item_id] if c in by_id]
            choices = ordered + [c for c in choices if c not in ordered]
        response = rmap.get(item_id)
        entries.append(
            {
                "number": number,
                "item": item,
                "question": item.question,
                "choices": choices,
                "response": response,
                "answer": response.get_answer() if response else None,
            }
        )
    return entries


def review_entries(attempt, staff=False):
    entries = ordered_entries(attempt)
    show_key = staff or attempt.assessment.show_correct_answers
    for entry in entries:
        question = entry["question"]
        answer = entry["answer"]
        selected = set(answer if isinstance(answer, list) else ([answer] if answer is not None else []))
        entry["selected"] = selected
        entry["show_key"] = show_key
        if question.qtype == "short":
            entry["key_text"] = ", ".join(c.text for c in question.choices)
        elif question.qtype == "numeric" and question.numeric_answer is not None:
            tol = question.numeric_tolerance
            entry["key_text"] = f"{question.numeric_answer:g}" + (f" (± {tol:g})" if tol else "")
        else:
            entry["key_text"] = ""
    return entries


def _own_attempt(attempt_id):
    attempt = db.session.get(Attempt, attempt_id)
    if attempt is None:
        abort(404)
    if attempt.user_id != current_user.id:
        if can_manage_course(attempt.assessment.course):
            return redirect(url_for("assessments.attempt_detail", assessment_id=attempt.assessment_id, attempt_id=attempt.id))
        abort(403)
    return attempt


def _clean_answer(item, values):
    """Validate a raw list of submitted values for one item into the stored answer format."""
    question = item.question
    values = [v for v in values if v is not None]
    if question.qtype in ("mc", "tf"):
        valid = {c.id for c in question.choices}
        for v in values:
            try:
                cid = int(v)
            except (TypeError, ValueError):
                continue
            if cid in valid:
                return cid
        return None
    if question.qtype == "ms":
        valid = {c.id for c in question.choices}
        picked = set()
        for v in values:
            try:
                cid = int(v)
            except (TypeError, ValueError):
                continue
            if cid in valid:
                picked.add(cid)
        return sorted(picked) or None
    text = (values[0] if values else "") or ""
    text = str(text)[:MAX_TEXT_ANSWER]
    return text if text.strip() else None


def _store_form_answers(attempt, form):
    for item in attempt.assessment.items:
        key = f"q_{item.id}"
        if key not in form and f"present_{item.id}" not in form:
            continue
        save_answer(attempt, item, _clean_answer(item, form.getlist(key)))


def _remaining_seconds(attempt):
    deadline = effective_deadline(attempt)
    if deadline is None:
        return None
    return max(0, int((deadline - utcnow()).total_seconds()))


@bp.route("/take/<int:assessment_id>", methods=["GET", "POST"])
@login_required
def start(assessment_id):
    assessment = db.session.get(Assessment, assessment_id)
    if assessment is None or assessment.status == "draft":
        abort(404)
    if can_manage_course(assessment.course):
        return redirect(url_for("assessments.preview", assessment_id=assessment.id))
    if not assessment.course.has_student(current_user):
        abort(403)
    attempts = [a for a in assessment.attempts if a.user_id == current_user.id]
    for attempt in attempts:
        enforce_deadline(attempt)
    db.session.commit()
    in_progress = next((a for a in attempts if a.status == "in_progress"), None)
    remaining = None if assessment.max_attempts == 0 else max(0, assessment.max_attempts - len(attempts))
    problem = None
    if not assessment.allow_online:
        problem = "This assessment is administered on paper."
    elif assessment.window_state() != "open":
        problem = "This assessment is not open right now."
    elif not assessment.items:
        problem = "This assessment has no questions yet."
    elif in_progress is None and remaining == 0:
        problem = "You have used all of your attempts."

    if request.method == "POST":
        if in_progress is not None:
            return redirect(url_for("student.take", attempt_id=in_progress.id))
        if problem:
            flash(problem, "error")
            return redirect(url_for("student.start", assessment_id=assessment.id))
        if assessment.access_code and request.form.get("access_code", "").strip() != assessment.access_code:
            log_event("attempt.bad_access_code", f"assessment #{assessment.id}")
            db.session.commit()
            flash("The access code is incorrect. Ask your proctor for the code.", "error")
            return redirect(url_for("student.start", assessment_id=assessment.id))
        attempt = start_attempt(assessment, current_user)
        log_event("attempt.started", f"assessment #{assessment.id}")
        db.session.commit()
        return redirect(url_for("student.take", attempt_id=attempt.id))
    return render_template(
        "student/start.html",
        assessment=assessment,
        attempts=attempts,
        in_progress=in_progress,
        remaining=remaining,
        problem=problem,
    )


@bp.route("/attempt/<int:attempt_id>")
@login_required
def take(attempt_id):
    attempt = _own_attempt(attempt_id)
    if not isinstance(attempt, Attempt):
        return attempt
    if enforce_deadline(attempt):
        db.session.commit()
        flash("Time is up — your saved answers were submitted automatically.", "info")
    if attempt.status != "in_progress":
        return redirect(url_for("student.review", attempt_id=attempt.id))
    return render_template(
        "student/take.html",
        assessment=attempt.assessment,
        attempt=attempt,
        entries=ordered_entries(attempt),
        preview=False,
        remaining=_remaining_seconds(attempt),
    )


@bp.route("/attempt/<int:attempt_id>/save", methods=["POST"])
@login_required
def autosave(attempt_id):
    attempt = _own_attempt(attempt_id)
    if not isinstance(attempt, Attempt):
        abort(403)
    if enforce_deadline(attempt):
        db.session.commit()
        return jsonify({"ok": False, "status": "closed", "redirect": url_for("student.review", attempt_id=attempt.id)}), 409
    if attempt.status != "in_progress":
        return jsonify({"ok": False, "status": "closed", "redirect": url_for("student.review", attempt_id=attempt.id)}), 409
    _store_form_answers(attempt, request.form)
    db.session.commit()
    return jsonify({"ok": True, "remaining": _remaining_seconds(attempt)})


@bp.route("/attempt/<int:attempt_id>/submit", methods=["POST"])
@login_required
def submit(attempt_id):
    attempt = _own_attempt(attempt_id)
    if not isinstance(attempt, Attempt):
        abort(403)
    if attempt.status == "in_progress":
        if enforce_deadline(attempt):
            flash("Time was up — your last saved answers were submitted.", "info")
        else:
            _store_form_answers(attempt, request.form)
            finalize_attempt(attempt)
            log_event("attempt.submitted", f"assessment #{attempt.assessment_id} attempt #{attempt.id}")
            flash("Your answers have been submitted.", "success")
        db.session.commit()
    return redirect(url_for("student.review", attempt_id=attempt.id))


@bp.route("/attempt/<int:attempt_id>/review")
@login_required
def review(attempt_id):
    attempt = _own_attempt(attempt_id)
    if not isinstance(attempt, Attempt):
        return attempt
    if attempt.status == "in_progress":
        return redirect(url_for("student.take", attempt_id=attempt.id))
    visible = attempt.assessment.results_visible()
    return render_template(
        "student/review.html",
        attempt=attempt,
        assessment=attempt.assessment,
        visible=visible,
        entries=review_entries(attempt) if visible else [],
    )


@bp.route("/my/progress")
@login_required
def my_progress():
    courses = visible_courses(current_user)
    rows = student_assessment_rows(current_user, courses)
    # Only use evidence from assessments whose results the student is allowed to see.
    standards, stats = student_mastery(current_user, courses, assessment_filter=lambda a: a.results_visible())
    visible_rows = [r for r in rows if r["counted"] and r["results_visible"]]
    return render_template("student/progress.html", rows=visible_rows, standards=standards, stats=stats)
