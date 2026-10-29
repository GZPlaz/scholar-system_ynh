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
    if keep_manual and response.manually_graded:
        return
    points, correct, pending = grade_answer(response.item, response.get_answer())
    response.points_awarded = points
    response.is_correct = correct
    response.needs_grading = pending
    if not keep_manual:
        response.manually_graded = False


def recompute_attempt(attempt):
    attempt.max_score = attempt.assessment.max_score
    attempt.score = round(sum(r.points_awarded or 0.0 for r in attempt.responses), 4)
    attempt.needs_grading = any(r.needs_grading for r in attempt.responses)


def finalize_attempt(attempt, when=None):
    """Grade every response and mark the attempt as submitted."""
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
        for response in attempt.responses:
            grade_response(response, keep_manual=keep_manual)
        recompute_attempt(attempt)
        count += 1
    return count


def start_attempt(assessment, user, source="online", entered_by=None):
    previous = Attempt.query.filter_by(assessment_id=assessment.id, user_id=user.id).count()
    item_ids = [item.id for item in assessment.items]
    if assessment.shuffle_questions and source == "online":
        random.shuffle(item_ids)
    choice_order = {}
    for item in assessment.items:
        if item.question.has_choices:
            ids = [c.id for c in item.question.choices]
            if assessment.shuffle_choices and source == "online" and item.question.qtype != "tf":
                random.shuffle(ids)
            choice_order[str(item.id)] = ids
    now = utcnow()
    deadline = None
    if source == "online":
        if assessment.time_limit_minutes:
            deadline = now + timedelta(minutes=assessment.time_limit_minutes)
        if assessment.closes_at and (deadline is None or assessment.closes_at < deadline):
            deadline = assessment.closes_at
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
    )
    db.session.add(attempt)
    return attempt


def effective_deadline(attempt):
    deadline = attempt.deadline
    closes = attempt.assessment.closes_at
    if attempt.source == "online" and closes and (deadline is None or closes < deadline):
        deadline = closes
    return deadline


def enforce_deadline(attempt, now=None):
    """Auto-submit an online attempt whose time has run out. Returns True if it was finalized."""
    if attempt.status != "in_progress" or attempt.source != "online":
        return False
    now = now or utcnow()
    deadline = effective_deadline(attempt)
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
        if attempt.status != "submitted":
            continue
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
