"""Scoring rules, attempt lifecycle and deadline enforcement."""
import json
import random
from datetime import timedelta

from .extensions import db
from .models import Attempt, Response, utcnow
from .utils import get_setting, parse_float

# Small grace period for network latency on timed submissions.
DEADLINE_GRACE = timedelta(seconds=20)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_text(value, case_sensitive=False):
    text = " ".join(str(value).split())
    return text if case_sensitive else text.casefold()


def is_blank(answer):
    if answer is None:
        return True
    if isinstance(answer, str):
        return not answer.strip()
    if isinstance(answer, (list, tuple)):
        return len(answer) == 0
    return False


def grade_answer(item, answer):
    """Score one answer. Returns ``(points_awarded, is_correct, needs_grading)``.

    ``points_awarded`` is ``None`` while a response awaits manual grading.
    """
    question = item.question
    points = item.effective_points
    qtype = question.qtype

    # A question fixed after the test: everyone gets it, or nobody is scored on it.
    if item.credit_mode == "all":
        return points, True, False
    if item.credit_mode == "drop":
        return 0.0, None, False

    if is_blank(answer):
        return 0.0, False, False

    if qtype == "essay":
        return None, None, True

    if qtype in ("mc", "tf"):
        if isinstance(answer, list):
            answer = answer[0] if answer else None
        chosen = _to_int(answer)
        correct_ids = {c.id for c in question.choices if c.is_correct}
        ok = chosen in correct_ids
        return (points if ok else 0.0), ok, False

    if qtype == "ms":
        values = answer if isinstance(answer, list) else [answer]
        valid = {c.id for c in question.choices}
        selected = {v for v in (_to_int(x) for x in values) if v in valid}
        correct = {c.id for c in question.choices if c.is_correct}
        if selected == correct:
            return points, True, False
        if not question.partial_credit or not correct:
            return 0.0, False, False
        hits = len(selected & correct)
        wrong = len(selected - correct)
        fraction = max(0.0, (hits - wrong) / len(correct))
        return round(points * fraction, 4), False, False

    if qtype == "short":
        accepted = {normalize_text(c.text, question.case_sensitive) for c in question.choices if c.text.strip()}
        if not accepted:
            return None, None, True
        ok = normalize_text(answer, question.case_sensitive) in accepted
        return (points if ok else 0.0), ok, False

    if qtype == "numeric":
        if question.numeric_answer is None:
            return None, None, True
        value = parse_float(answer)
        if value is None:
            return 0.0, False, False
        tolerance = max(question.numeric_tolerance or 0.0, 1e-9)
        ok = abs(value - question.numeric_answer) <= tolerance
        return (points if ok else 0.0), ok, False

    return None, None, True


def grade_response(response, keep_manual=True):
    if keep_manual and response.manually_graded and not response.item.credit_mode:
        return
    points, correct, pending = grade_answer(response.item, response.get_answer())
    response.points_awarded = points
    response.is_correct = correct
    response.needs_grading = pending
    if not keep_manual:
        response.manually_graded = False


def _cover_credited_items(attempt):
    """A question everyone gets credit for counts for students who left it blank too."""
    answered = {r.item_id for r in attempt.responses}
    for item in attempt.assessment.items:
        if item.credit_mode == "all" and item.id not in answered:
            db.session.add(Response(attempt=attempt, item=item))


def recompute_attempt(attempt):
    attempt.max_score = attempt.assessment.max_score
    attempt.score = round(sum(r.points_awarded or 0.0 for r in attempt.responses), 4)
    attempt.needs_grading = any(r.needs_grading for r in attempt.responses)


def finalize_attempt(attempt, when=None):
    """Grade every response and mark the attempt as submitted."""
    _cover_credited_items(attempt)
    for response in attempt.responses:
        grade_response(response)
    attempt.status = "submitted"
    attempt.submitted_at = when or utcnow()
    recompute_attempt(attempt)


def regrade_assessment(assessment, keep_manual=True):
    count = 0
    for attempt in assessment.attempts:
        if attempt.status != "submitted":
            continue
        _cover_credited_items(attempt)
        for response in attempt.responses:
            grade_response(response, keep_manual=keep_manual)
        recompute_attempt(attempt)
        count += 1
    return count


def start_attempt(assessment, user, source="online", entered_by=None, retake=False):
    previous = Attempt.query.filter_by(assessment_id=assessment.id, user_id=user.id).count()
    item_ids = [item.id for item in assessment.items]
    if assessment.shuffle_questions and source == "online" and not assessment.pdf:  # a PDF's numbering is fixed
        random.shuffle(item_ids)
    choice_order = {}
    for item in assessment.items:
        if item.question.has_choices:
            ids = [c.id for c in item.question.choices]
            if assessment.shuffle_choices and source == "online" and item.question.qtype != "tf" and not assessment.pdf:
                random.shuffle(ids)
            choice_order[str(item.id)] = ids
    now = utcnow()
    deadline = None
    if source == "online":
        if assessment.time_limit_minutes:
            deadline = now + timedelta(minutes=assessment.time_limit_minutes)
        # (a retake happens after the closing time, so it is bounded by the retake window instead)
        end = assessment.retake_closes_at if retake else assessment.closes_at
        if end and (deadline is None or end < deadline):
            deadline = end
    attempt = Attempt(
        assessment=assessment,
        user=user,
        number=previous + 1,
        source=source,
        status="in_progress",
        started_at=now,
        deadline=deadline,
        max_score=assessment.max_score,
        item_order=json.dumps(item_ids),
        choice_order=json.dumps(choice_order),
        entered_by=entered_by,
        is_retake=retake,
        practice=retake and assessment.retake_mode == "practice",
    )
    db.session.add(attempt)
    if retake and assessment.retake_prefill:
        prefill_answers(attempt)
    return attempt


def prefill_answers(attempt):
    """Start a retake with the answers the student gave last time (they can change any of them)."""
    earlier = [
        a
        for a in attempt.assessment.attempts
        if a.user_id == attempt.user.id and a.status == "submitted" and a is not attempt
    ]
    if not earlier:
        return 0
    last = max(earlier, key=lambda a: (a.submitted_at or a.started_at, a.number))
    items = {i.id: i for i in attempt.assessment.items}
    count = 0
    for response in last.responses:
        answer = response.get_answer()
        if response.item_id in items and answer is not None:
            save_answer(attempt, items[response.item_id], answer)
            count += 1
    return count


def effective_deadline(attempt):
    """When an online attempt ends: its time limit or the closing time, plus any time a proctor added."""
    deadline = attempt.deadline
    if attempt.is_retake:
        end = attempt.assessment.retake_closes_at
        if end and (deadline is None or end < deadline):
            deadline = end
        if deadline is not None and attempt.extra_minutes:
            deadline += timedelta(minutes=attempt.extra_minutes)
        return deadline
    closes = attempt.assessment.closes_at
    if attempt.source == "online" and closes and (deadline is None or closes < deadline):
        deadline = closes
    if deadline is not None and attempt.extra_minutes:
        deadline += timedelta(minutes=attempt.extra_minutes)
    return deadline


def enforce_deadline(attempt, now=None):
    """Auto-submit an online attempt whose time has run out. Returns True if it was finalized."""
    if attempt.status != "in_progress" or attempt.source != "online":
        return False
    now = now or utcnow()
    deadline = effective_deadline(attempt)
    if attempt.is_retake:
        # Closing the test is what lets retakes start, so it must not end one. A retake ends when its
        # time runs out, its window closes, or the teacher turns retakes off.
        if not attempt.assessment.retake_mode:
            finalize_attempt(attempt, when=now)
            return True
        if deadline and now > deadline + DEADLINE_GRACE:
            finalize_attempt(attempt, when=deadline)
            return True
        return False
    if attempt.assessment.status == "closed":
        finalize_attempt(attempt, when=min(now, deadline) if deadline else now)
        return True
    if deadline and now > deadline + DEADLINE_GRACE:
        finalize_attempt(attempt, when=deadline)
        return True
    return False


def enforce_deadlines(attempts):
    changed = False
    for attempt in attempts:
        changed = enforce_deadline(attempt) or changed
    if changed:
        db.session.commit()
    return changed


def save_answer(attempt, item, answer):
    response = next((r for r in attempt.responses if r.item_id == item.id), None)
    if response is None:
        response = Response(attempt=attempt, item=item)
        db.session.add(response)
    response.set_answer(answer)
    return response


def counted_attempts(assessment, policy=None):
    """Map user_id → the submitted attempt that counts for reporting (best or latest)."""
    policy = policy or get_setting("attempt_policy") or "best"
    chosen = {}
    for attempt in assessment.attempts:
        if attempt.status != "submitted" or attempt.practice:
            continue  # a practice retake never changes anyone's grade
        current = chosen.get(attempt.user_id)
        if current is None:
            chosen[attempt.user_id] = attempt
            continue
        if policy == "latest":
            if (attempt.submitted_at or attempt.started_at) > (current.submitted_at or current.started_at):
                chosen[attempt.user_id] = attempt
        elif (attempt.percent, attempt.number) > (current.percent, current.number):
            chosen[attempt.user_id] = attempt
    return chosen
