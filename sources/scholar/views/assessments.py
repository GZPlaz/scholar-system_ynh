from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..analytics import item_analysis, score_summary
from ..extensions import db
from ..grading import (
    counted_attempts,
    enforce_deadlines,
    finalize_attempt,
    grade_response,
    recompute_attempt,
    regrade_assessment,
    save_answer,
    start_attempt,
)
from ..models import (
    QTYPES,
    SHOW_RESULTS,
    Assessment,
    AssessmentItem,
    Attempt,
    Course,
    Question,
    Response,
    Standard,
    User,
    utcnow,
)
from ..utils import (
    can_manage_course,
    csv_response,
    fmt_num,
    format_dt,
    log_event,
    parse_bool,
    parse_float,
    parse_int,
    parse_local_datetime,
    read_csv_upload,
    require_course_manager,
    safe_filename,
    staff_required,
)
from .questions import visible_questions_query

bp = Blueprint("assessments", __name__, url_prefix="/assessments")


def get_assessment(assessment_id):
    assessment = db.session.get(Assessment, assessment_id)
    if assessment is None:
        abort(404)
    require_course_manager(assessment.course)
    return assessment


def get_attempt(assessment, attempt_id):
    attempt = db.session.get(Attempt, attempt_id)
    if attempt is None or attempt.assessment_id != assessment.id:
        abort(404)
    return attempt


def _normalize_positions(assessment):
    for idx, item in enumerate(sorted(assessment.items, key=lambda i: (i.position, i.id)), start=1):
        item.position = idx


# --------------------------------------------------------------------------- create / settings


def _apply_settings(assessment, form):
    errors = []
    title = form.get("title", "").strip()
    if not title:
        errors.append("A title is required.")
    opens_at = parse_local_datetime(form.get("opens_at"))
    closes_at = parse_local_datetime(form.get("closes_at"))
    if opens_at and closes_at and closes_at <= opens_at:
        errors.append("The closing time must be after the opening time.")
    time_limit = parse_int(form.get("time_limit_minutes"))
    if time_limit is not None and time_limit <= 0:
        time_limit = None
    max_attempts = parse_int(form.get("max_attempts"), 1)
    if max_attempts is None or max_attempts < 0:
        errors.append("Attempts allowed must be 0 (unlimited) or more.")
    show_results = form.get("show_results", "immediately")
    if show_results not in SHOW_RESULTS:
        errors.append("Invalid results policy.")
    if errors:
        return errors
    assessment.title = title[:200]
    assessment.instructions = form.get("instructions", "").strip()
    assessment.opens_at = opens_at
    assessment.closes_at = closes_at
    assessment.time_limit_minutes = time_limit
    assessment.max_attempts = max_attempts
    assessment.shuffle_questions = parse_bool(form.get("shuffle_questions", ""))
    assessment.shuffle_choices = parse_bool(form.get("shuffle_choices", ""))
    assessment.show_results = show_results
    assessment.show_correct_answers = parse_bool(form.get("show_correct_answers", ""))
    assessment.access_code = form.get("access_code", "").strip()[:60]
    assessment.allow_online = parse_bool(form.get("allow_online", ""))
    return []


@bp.route("/new", methods=["GET", "POST"])
@staff_required
def new():
    course = db.session.get(Course, parse_int(request.values.get("course_id"), 0))
    if course is None:
        abort(404)
    require_course_manager(course)
    if request.method == "POST":
        assessment = Assessment()
        errors = _apply_settings(assessment, request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("assessments/settings.html", assessment=None, course=course, form=request.form)
        assessment.course = course
        assessment.owner = current_user
        db.session.add(assessment)
        log_event("assessment.created", f"{course.code}: {assessment.title}")
        db.session.commit()
        flash("Assessment created. Now add questions.", "success")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    defaults = {"max_attempts": "1", "show_results": "immediately", "allow_online": "1"}
    return render_template("assessments/settings.html", assessment=None, course=course, form=defaults)


@bp.route("/<int:assessment_id>/settings", methods=["GET", "POST"])
@staff_required
def settings(assessment_id):
    assessment = get_assessment(assessment_id)
    if request.method == "POST":
        errors = _apply_settings(assessment, request.form)
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
            return render_template("assessments/settings.html", assessment=assessment, course=assessment.course, form=request.form)
        log_event("assessment.settings", f"#{assessment.id}")
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    return render_template("assessments/settings.html", assessment=assessment, course=assessment.course, form=None)


# --------------------------------------------------------------------------- builder


@bp.route("/<int:assessment_id>")
@staff_required
def detail(assessment_id):
    assessment = get_assessment(assessment_id)
    enforce_deadlines([a for a in assessment.attempts if a.status == "in_progress"])
    in_assessment = {item.question_id for item in assessment.items}
    bank = None
    filters = {
        "q": request.args.get("q", "").strip(),
        "type": request.args.get("type", ""),
        "standard": parse_int(request.args.get("standard")),
    }
    if request.args.get("bank") and not assessment.is_locked:
        query = visible_questions_query().filter(Question.archived.is_(False))
        if filters["q"]:
            like = f"%{filters['q'].lower()}%"
            query = query.filter(db.or_(db.func.lower(Question.stem).like(like), db.func.lower(Question.topic).like(like)))
        if filters["type"] in QTYPES:
            query = query.filter(Question.qtype == filters["type"])
        if filters["standard"]:
            query = query.filter(Question.standards.any(Standard.id == filters["standard"]))
        bank = [q for q in query.order_by(Question.updated_at.desc()).limit(200) if q.id not in in_assessment]
    standards = Standard.query.order_by(Standard.code).all()
    coverage = {}
    for item in assessment.items:
        for std in item.question.standards:
            entry = coverage.setdefault(std.id, {"standard": std, "items": 0, "points": 0.0})
            entry["items"] += 1
            entry["points"] += item.effective_points
    other_courses = [c for c in Course.query.filter_by(archived=False).order_by(Course.code) if can_manage_course(c)]
    return render_template(
        "assessments/detail.html",
        assessment=assessment,
        bank=bank,
        filters=filters,
        standards=standards,
        coverage=sorted(coverage.values(), key=lambda e: e["standard"].code),
        other_courses=other_courses,
        counts={
            "submitted": sum(1 for a in assessment.attempts if a.status == "submitted"),
            "in_progress": sum(1 for a in assessment.attempts if a.status == "in_progress"),
            "to_grade": sum(1 for a in assessment.attempts if a.needs_grading and a.status == "submitted"),
        },
    )


@bp.route("/<int:assessment_id>/items/add", methods=["POST"])
@staff_required
def add_items(assessment_id):
    assessment = get_assessment(assessment_id)
    if assessment.is_locked:
        flash("Students have already started this assessment; its questions can no longer change.", "error")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    existing = {item.question_id for item in assessment.items}
    position = max([i.position for i in assessment.items], default=0)
    added = 0
    allowed = {q.id for q in visible_questions_query().filter(Question.id.in_([parse_int(x) for x in request.form.getlist("question_ids")]))}
    for qid in request.form.getlist("question_ids"):
        qid = parse_int(qid)
        if qid in allowed and qid not in existing:
            position += 1
            db.session.add(AssessmentItem(assessment=assessment, question_id=qid, position=position))
            existing.add(qid)
            added += 1
    db.session.commit()
    flash(f"{added} question(s) added.", "success")
    return redirect(url_for("assessments.detail", assessment_id=assessment.id))


@bp.route("/<int:assessment_id>/items/<int:item_id>/<action>", methods=["POST"])
@staff_required
def item_action(assessment_id, item_id, action):
    assessment = get_assessment(assessment_id)
    item = db.session.get(AssessmentItem, item_id)
    if item is None or item.assessment_id != assessment.id:
        abort(404)
    if action == "points":
        value = request.form.get("points", "").strip()
        points = parse_float(value) if value else None
        if points is not None and points <= 0:
            flash("Points must be positive.", "error")
        else:
            item.points = points
            db.session.flush()
            regraded = regrade_assessment(assessment)
            db.session.commit()
            flash("Points updated." + (f" {regraded} attempt(s) re-scored." if regraded else ""), "success")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    if assessment.is_locked:
        flash("Students have already started this assessment; question order and membership are locked.", "error")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    _normalize_positions(assessment)
    items = sorted(assessment.items, key=lambda i: i.position)
    idx = items.index(item)
    if action == "remove":
        db.session.delete(item)
        assessment.items.remove(item)
        _normalize_positions(assessment)
    elif action == "up" and idx > 0:
        items[idx - 1].position, item.position = item.position, items[idx - 1].position
    elif action == "down" and idx < len(items) - 1:
        items[idx + 1].position, item.position = item.position, items[idx + 1].position
    db.session.commit()
    return redirect(url_for("assessments.detail", assessment_id=assessment.id) + "#items")


@bp.route("/<int:assessment_id>/status", methods=["POST"])
@staff_required
def change_status(assessment_id):
    assessment = get_assessment(assessment_id)
    action = request.form.get("action")
    if action == "publish":
        if not assessment.items:
            flash("Add at least one question before publishing.", "error")
        else:
            assessment.status = "published"
            flash("Assessment published. Students can take it during its availability window.", "success")
    elif action == "unpublish":
        if assessment.attempts:
            flash("Students have already started; close the assessment instead.", "error")
        else:
            assessment.status = "draft"
            flash("Assessment moved back to draft.", "success")
    elif action == "close":
        assessment.status = "closed"
        db.session.flush()
        enforce_deadlines([a for a in assessment.attempts if a.status == "in_progress"])
        flash("Assessment closed. Any in-progress attempts were submitted.", "success")
    elif action == "reopen":
        assessment.status = "published"
        if assessment.closes_at and assessment.closes_at <= utcnow():
            flash("Reopened — note that the closing date has passed; edit the settings to extend it.", "info")
        else:
            flash("Assessment reopened.", "success")
    elif action == "release":
        assessment.results_released = not assessment.results_released
        flash("Results are now " + ("visible" if assessment.results_released else "hidden") + " to students.", "success")
    else:
        abort(400)
    log_event(f"assessment.{action}", f"#{assessment.id}")
    db.session.commit()
    return redirect(request.form.get("next") or url_for("assessments.detail", assessment_id=assessment.id))


@bp.route("/<int:assessment_id>/duplicate", methods=["POST"])
@staff_required
def duplicate(assessment_id):
    source = get_assessment(assessment_id)
    course = db.session.get(Course, parse_int(request.form.get("course_id"), source.course_id)) or source.course
    require_course_manager(course)
    copy = Assessment(
        title=(source.title + " (copy)")[:200] if course.id == source.course_id else source.title,
        instructions=source.instructions,
        time_limit_minutes=source.time_limit_minutes,
        max_attempts=source.max_attempts,
        shuffle_questions=source.shuffle_questions,
        shuffle_choices=source.shuffle_choices,
        show_results=source.show_results,
        show_correct_answers=source.show_correct_answers,
        allow_online=source.allow_online,
    )
    db.session.add(copy)
    copy.course = course
    copy.owner = current_user
    for item in source.items:
        copy.items.append(AssessmentItem(question_id=item.question_id, position=item.position, points=item.points))
    log_event("assessment.duplicated", f"#{source.id} -> {course.code}")
    db.session.commit()
    flash("Assessment duplicated as a draft (dates were cleared).", "success")
    return redirect(url_for("assessments.detail", assessment_id=copy.id))


@bp.route("/<int:assessment_id>/delete", methods=["POST"])
@staff_required
def delete(assessment_id):
    assessment = get_assessment(assessment_id)
    course_id = assessment.course_id
    if assessment.attempts and request.form.get("confirm") != assessment.title:
        flash("This assessment has student results. Type its exact title to confirm deletion.", "error")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    log_event("assessment.deleted", f"#{assessment.id} {assessment.title}")
    db.session.delete(assessment)
    db.session.commit()
    flash("Assessment deleted.", "success")
    return redirect(url_for("courses.detail", course_id=course_id))


@bp.route("/<int:assessment_id>/preview")
@staff_required
def preview(assessment_id):
    assessment = get_assessment(assessment_id)
    entries = [{"item": item, "choices": item.question.choices, "answer": None} for item in assessment.items]
    return render_template("student/take.html", assessment=assessment, attempt=None, entries=entries, preview=True, remaining=None)


# --------------------------------------------------------------------------- results


def _result_rows(assessment):
    counted = counted_attempts(assessment)
    by_user = {}
    for attempt in assessment.attempts:
        by_user.setdefault(attempt.user_id, []).append(attempt)
    users = {u.id: u for u in assessment.course.students}
    for attempt in assessment.attempts:
        users.setdefault(attempt.user_id, attempt.user)
    rows = []
    for user in sorted(users.values(), key=lambda u: u.sort_key):
        attempts = sorted(by_user.get(user.id, []), key=lambda a: a.number)
        counted_attempt = counted.get(user.id)
        if counted_attempt:
            status = "needs grading" if counted_attempt.needs_grading else "submitted"
        elif any(a.status == "in_progress" for a in attempts):
            status = "in progress"
        else:
            status = "not started"
        rows.append(
            {
                "user": user,
                "attempts": attempts,
                "counted": counted_attempt,
                "status": status,
                "enrolled": user.id in {s.id for s in assessment.course.students},
            }
        )
    return rows


@bp.route("/<int:assessment_id>/results")
@staff_required
def results(assessment_id):
    assessment = get_assessment(assessment_id)
    enforce_deadlines([a for a in assessment.attempts if a.status == "in_progress"])
    rows = _result_rows(assessment)
    if request.args.get("format") == "csv":
        items = list(assessment.items)
        header = ["username", "full_name", "student_number", "status", "attempt", "source", "submitted_at", "score", "max_score", "percent"]
        header += [f"Q{i + 1}" for i in range(len(items))]
        data = []
        for row in rows:
            attempt = row["counted"]
            line = [row["user"].username, row["user"].full_name, row["user"].student_number, row["status"]]
            if attempt:
                rmap = attempt.response_map()
                line += [attempt.number, attempt.source, format_dt(attempt.submitted_at, "%Y-%m-%d %H:%M"), fmt_num(attempt.score), fmt_num(attempt.max_score), f"{attempt.percent:.1f}"]
                line += [fmt_num(rmap[i.id].points_awarded) if i.id in rmap and rmap[i.id].points_awarded is not None else "" for i in items]
            else:
                line += ["", "", "", "", fmt_num(assessment.max_score), ""] + [""] * len(items)
            data.append(line)
        return csv_response(f"{safe_filename(assessment.course.code + '_' + assessment.title)}_results.csv", header, data)
    summary = score_summary([r["counted"].percent for r in rows if r["counted"]])
    return render_template("assessments/results.html", assessment=assessment, rows=rows, summary=summary)


@bp.route("/<int:assessment_id>/analysis")
@staff_required
def analysis(assessment_id):
    assessment = get_assessment(assessment_id)
    data = item_analysis(assessment)
    if request.args.get("format") == "csv":
        header = ["item", "type", "standards", "points", "p_value", "point_biserial", "discrimination_D", "omitted", "flags", "question"]
        lines = []
        for entry in data["items"]:
            lines.append(
                [
                    entry["number"],
                    entry["question"].qtype,
                    " ".join(s.code for s in entry["standards"]),
                    fmt_num(entry["item"].effective_points),
                    "" if entry["p_value"] is None else f"{entry['p_value']:.3f}",
                    "" if entry["rpb"] is None else f"{entry['rpb']:.3f}",
                    "" if entry["d_index"] is None else f"{entry['d_index']:.3f}",
                    entry["omitted"],
                    "; ".join(entry["flags"]),
                    entry["question"].stem_preview,
                ]
            )
        return csv_response(f"{safe_filename(assessment.title)}_item_analysis.csv", header, lines)
    return render_template("assessments/analysis.html", assessment=assessment, data=data)


@bp.route("/<int:assessment_id>/regrade", methods=["POST"])
@staff_required
def regrade(assessment_id):
    assessment = get_assessment(assessment_id)
    keep_manual = not parse_bool(request.form.get("reset_manual", ""))
    count = regrade_assessment(assessment, keep_manual=keep_manual)
    log_event("assessment.regraded", f"#{assessment.id} attempts={count}")
    db.session.commit()
    flash(f"{count} submitted attempt(s) re-scored against the current answer key.", "success")
    return redirect(url_for("assessments.results", assessment_id=assessment.id))


# --------------------------------------------------------------------------- grading


@bp.route("/<int:assessment_id>/grading")
@staff_required
def grading(assessment_id):
    assessment = get_assessment(assessment_id)
    pending = [a for a in assessment.attempts if a.status == "submitted" and a.needs_grading]
    pending.sort(key=lambda a: a.submitted_at or a.started_at)
    per_item = []
    for item in assessment.items:
        count = Response.query.filter_by(item_id=item.id, needs_grading=True).count()
        if count or item.question.qtype in ("essay",):
            per_item.append({"item": item, "pending": count})
    return render_template("assessments/grading.html", assessment=assessment, pending=pending, per_item=per_item)


def _apply_manual_grade(response, points_raw, feedback, max_points):
    changed = False
    if feedback is not None and feedback != response.feedback:
        response.feedback = feedback.strip()
        changed = True
    points_raw = (points_raw or "").strip()
    if points_raw == "":
        return changed
    points = parse_float(points_raw)
    if points is None:
        return changed
    points = max(0.0, min(points, max_points))
    if response.points_awarded is None or abs(points - response.points_awarded) > 1e-9 or response.needs_grading:
        response.points_awarded = points
        response.is_correct = points >= max_points - 1e-9
        response.needs_grading = False
        response.manually_graded = True
        response.graded_by_id = current_user.id
        response.graded_at = utcnow()
        changed = True
    return changed


@bp.route("/<int:assessment_id>/grading/item/<int:item_id>", methods=["GET", "POST"])
@staff_required
def grade_item(assessment_id, item_id):
    """Grade every student's response to one question (fast essay grading)."""
    assessment = get_assessment(assessment_id)
    item = db.session.get(AssessmentItem, item_id)
    if item is None or item.assessment_id != assessment.id:
        abort(404)
    show_all = parse_bool(request.args.get("all", ""))
    responses = [r for r in item.responses if r.attempt.status == "submitted" and (show_all or r.needs_grading)]
    responses.sort(key=lambda r: r.attempt.user.sort_key)
    if request.method == "POST":
        touched = set()
        for response in item.responses:
            if f"points_{response.id}" not in request.form:
                continue
            if _apply_manual_grade(
                response, request.form.get(f"points_{response.id}"), request.form.get(f"feedback_{response.id}"), item.effective_points
            ):
                touched.add(response.attempt)
        for attempt in touched:
            recompute_attempt(attempt)
        log_event("grading.item", f"assessment #{assessment.id} item #{item.id} responses={len(touched)}")
        db.session.commit()
        flash(f"Saved grades for {len(touched)} response(s).", "success")
        return redirect(url_for("assessments.grade_item", assessment_id=assessment.id, item_id=item.id, all=int(show_all)))
    number = [i.id for i in assessment.items].index(item.id) + 1
    return render_template("assessments/grade_item.html", assessment=assessment, item=item, number=number, responses=responses, show_all=show_all)


@bp.route("/<int:assessment_id>/attempts/<int:attempt_id>", methods=["GET", "POST"])
@staff_required
def attempt_detail(assessment_id, attempt_id):
    assessment = get_assessment(assessment_id)
    attempt = get_attempt(assessment, attempt_id)
    if request.method == "POST":
        if attempt.status != "submitted":
            flash("This attempt is still in progress.", "error")
            return redirect(url_for("assessments.attempt_detail", assessment_id=assessment.id, attempt_id=attempt.id))
        rmap = attempt.response_map()
        for item in assessment.items:
            if f"points_{item.id}" not in request.form:
                continue
            response = rmap.get(item.id)
            points_raw = request.form.get(f"points_{item.id}")
            feedback = request.form.get(f"feedback_{item.id}", "")
            if response is None:
                if not (points_raw or "").strip() and not feedback.strip():
                    continue
                response = save_answer(attempt, item, None)
                response.points_awarded = 0.0
                response.is_correct = False
            _apply_manual_grade(response, points_raw, feedback, item.effective_points)
        recompute_attempt(attempt)
        log_event("grading.attempt", f"attempt #{attempt.id} ({attempt.user.username})")
        db.session.commit()
        flash("Grades saved.", "success")
        nxt = next(
            (a for a in sorted(assessment.attempts, key=lambda a: a.submitted_at or a.started_at) if a.needs_grading and a.status == "submitted"),
            None,
        )
        if request.form.get("next_pending") and nxt:
            return redirect(url_for("assessments.attempt_detail", assessment_id=assessment.id, attempt_id=nxt.id))
        return redirect(url_for("assessments.attempt_detail", assessment_id=assessment.id, attempt_id=attempt.id))
    from .student import review_entries

    return render_template("assessments/attempt.html", assessment=assessment, attempt=attempt, entries=review_entries(attempt, True))


@bp.route("/<int:assessment_id>/attempts/<int:attempt_id>/delete", methods=["POST"])
@staff_required
def delete_attempt(assessment_id, attempt_id):
    assessment = get_assessment(assessment_id)
    attempt = get_attempt(assessment, attempt_id)
    log_event("attempt.deleted", f"attempt #{attempt.id} of {attempt.user.username} on assessment #{assessment.id}")
    db.session.delete(attempt)
    db.session.flush()
    # keep attempt numbers contiguous for that student
    for idx, other in enumerate(sorted((a for a in assessment.attempts if a.user_id == attempt.user_id and a is not attempt), key=lambda a: a.started_at), start=1):
        other.number = idx
    db.session.commit()
    flash("Attempt deleted — the student can take the assessment again.", "success")
    return redirect(url_for("assessments.results", assessment_id=assessment.id))


# --------------------------------------------------------------------------- paper tests


@bp.route("/<int:assessment_id>/print")
@staff_required
def print_test(assessment_id):
    assessment = get_assessment(assessment_id)
    return render_template("assessments/print.html", assessment=assessment, key=parse_bool(request.args.get("key", "")))


@bp.route("/<int:assessment_id>/bubble-sheets")
@staff_required
def bubble_sheets(assessment_id):
    assessment = get_assessment(assessment_id)
    students = assessment.course.students if not parse_bool(request.args.get("blank", "")) else []
    max_choices = max([len(i.question.choices) for i in assessment.items if i.question.has_choices] or [4])
    return render_template("assessments/bubbles.html", assessment=assessment, students=students or [None], max_choices=max(4, max_choices))


def letters_to_answer(item, raw):
    """Convert paper/keyed entry (letters, T/F, text) into the stored answer format."""
    question = item.question
    raw = (raw or "").strip()
    if not raw:
        return None
    if question.qtype in ("mc", "tf"):
        token = raw.upper()
        if question.qtype == "tf" and token in ("T", "TRUE", "V", "VRAI"):
            token = "A"
        elif question.qtype == "tf" and token in ("F", "FALSE", "FAUX"):
            token = "B"
        if token.isdigit():
            idx = int(token) - 1
        elif len(token) == 1 and token.isalpha():
            idx = ord(token) - 65
        else:
            raise ValueError(f"'{raw}' is not a valid choice letter")
        if not 0 <= idx < len(question.choices):
            raise ValueError(f"'{raw}' is outside the options A–{chr(64 + len(question.choices))}")
        return question.choices[idx].id
    if question.qtype == "ms":
        ids = []
        for token in raw.upper().replace(",", "").replace(" ", "").replace("|", ""):
            idx = ord(token) - 65
            if not 0 <= idx < len(question.choices):
                raise ValueError(f"'{token}' is outside the options A–{chr(64 + len(question.choices))}")
            ids.append(question.choices[idx].id)
        return sorted(set(ids))
    return raw


def answer_to_letters(item, answer):
    question = item.question
    if answer is None:
        return ""
    if question.has_choices:
        ids = answer if isinstance(answer, list) else [answer]
        index = {c.id: chr(65 + i) for i, c in enumerate(question.choices)}
        return "".join(index.get(x, "?") for x in ids)
    return str(answer)


def record_paper_attempt(assessment, user, values):
    """values: {item_id: raw string}. Essay items take a points value. Returns the attempt.

    Everything is validated before the database is touched, so a ValueError leaves no trace.
    """
    parsed = {}
    for number, item in enumerate(assessment.items, start=1):
        raw = (values.get(item.id) or "").strip()
        try:
            if item.question.qtype == "essay":
                points = parse_float(raw) if raw else None
                if raw and points is None:
                    raise ValueError(f"essay points '{raw}' must be a number")
                parsed[item.id] = points
            else:
                parsed[item.id] = letters_to_answer(item, raw)
        except ValueError as exc:
            raise ValueError(f"question {number}: {exc}") from None

    attempt = next((a for a in assessment.attempts if a.user_id == user.id and a.source == "paper"), None)
    if attempt is None:
        attempt = start_attempt(assessment, user, source="paper", entered_by=current_user)
    for item in assessment.items:
        value = parsed[item.id]
        if item.question.qtype == "essay":
            response = next((r for r in attempt.responses if r.item_id == item.id), None)
            if value is None:
                if response is None:
                    response = save_answer(attempt, item, "[paper]")
                    response.needs_grading = True
                    response.manually_graded = True  # keep it pending until a grader scores it
                continue
            response = response or save_answer(attempt, item, "[paper]")
            response.set_answer("[paper]")
            response.points_awarded = max(0.0, min(value, item.effective_points))
            response.is_correct = response.points_awarded >= item.effective_points
            response.needs_grading = False
            response.manually_graded = True
            response.graded_by_id = current_user.id
            response.graded_at = utcnow()
            continue
        response = save_answer(attempt, item, value)
        response.manually_graded = False
    attempt.entered_by = current_user
    finalize_attempt(attempt)
    return attempt


@bp.route("/<int:assessment_id>/paper")
@staff_required
def paper(assessment_id):
    assessment = get_assessment(assessment_id)
    rows = []
    for student in assessment.course.students:
        attempt = next((a for a in assessment.attempts if a.user_id == student.id and a.source == "paper"), None)
        rows.append({"user": student, "attempt": attempt})
    return render_template("assessments/paper.html", assessment=assessment, rows=rows)


@bp.route("/<int:assessment_id>/paper/<int:user_id>", methods=["GET", "POST"])
@staff_required
def paper_entry(assessment_id, user_id):
    assessment = get_assessment(assessment_id)
    user = db.session.get(User, user_id)
    if user is None or not assessment.course.has_student(user):
        abort(404)
    if not assessment.items:
        flash("This assessment has no questions yet.", "error")
        return redirect(url_for("assessments.detail", assessment_id=assessment.id))
    attempt = next((a for a in assessment.attempts if a.user_id == user.id and a.source == "paper"), None)
    if request.method == "POST":
        values = {item.id: request.form.get(f"q_{item.id}", "") for item in assessment.items}
        try:
            record_paper_attempt(assessment, user, values)
        except ValueError as exc:
            db.session.rollback()
            flash(f"Not saved: {exc}", "error")
            return render_template("assessments/paper_entry.html", assessment=assessment, user=user, values=values, attempt=attempt)
        log_event("paper.entered", f"assessment #{assessment.id} {user.username}")
        db.session.commit()
        flash(f"Responses saved for {user.display_name}.", "success")
        students = assessment.course.students
        idx = [s.id for s in students].index(user.id)
        if request.form.get("save_next") and idx + 1 < len(students):
            return redirect(url_for("assessments.paper_entry", assessment_id=assessment.id, user_id=students[idx + 1].id))
        return redirect(url_for("assessments.paper", assessment_id=assessment.id))
    values = {}
    if attempt:
        rmap = attempt.response_map()
        for item in assessment.items:
            r = rmap.get(item.id)
            if r is None:
                continue
            if item.question.qtype == "essay":
                values[item.id] = fmt_num(r.points_awarded) if r.points_awarded is not None and not r.needs_grading else ""
            else:
                values[item.id] = answer_to_letters(item, r.get_answer())
    return render_template("assessments/paper_entry.html", assessment=assessment, user=user, values=values, attempt=attempt)


@bp.route("/<int:assessment_id>/paper/import", methods=["POST"])
@staff_required
def paper_import(assessment_id):
    assessment = get_assessment(assessment_id)
    rows = read_csv_upload()
    if rows is None:
        return redirect(url_for("assessments.paper", assessment_id=assessment.id))
    items = list(assessment.items)
    students = {s.username.lower(): s for s in assessment.course.students}
    by_number = {s.student_number: s for s in assessment.course.students if s.student_number}
    saved, errors = 0, []
    for lineno, row in enumerate(rows, start=2):
        key = row.get("username") or row.get("student_id") or row.get("student_number") or ""
        user = students.get(key.lower()) or by_number.get(key)
        if user is None:
            errors.append(f"Line {lineno}: '{key}' is not enrolled in this course")
            continue
        values = {}
        for n, item in enumerate(items, start=1):
            values[item.id] = row.get(f"q{n}", row.get(str(n), ""))
        try:
            record_paper_attempt(assessment, user, values)
            saved += 1
        except ValueError as exc:
            errors.append(f"Line {lineno} ({key}): {exc}")
    log_event("paper.import", f"assessment #{assessment.id} saved={saved}")
    db.session.commit()
    flash(f"Imported responses for {saved} student(s).", "success")
    for err in errors[:25]:
        flash(err, "error")
    return redirect(url_for("assessments.paper", assessment_id=assessment.id))
