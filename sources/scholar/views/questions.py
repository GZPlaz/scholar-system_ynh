import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..extensions import db
from ..grading import regrade_assessment
from ..models import CHOICE_TYPES, QTYPES, Assessment, AssessmentItem, Choice, Question, Response, Standard
from ..utils import (
    can_edit_question,
    can_manage_course,
    can_view_question,
    log_event,
    parse_bool,
    parse_float,
    parse_int,
    read_csv_upload,
    staff_required,
)

bp = Blueprint("questions", __name__, url_prefix="/questions")


def get_question(question_id, edit=False):
    question = db.session.get(Question, question_id)
    if question is None:
        abort(404)
    if not (can_edit_question(question) if edit else can_view_question(question)):
        abort(403)
    return question


def question_has_responses(question):
    return (
        db.session.query(Response.id)
        .join(AssessmentItem)
        .filter(AssessmentItem.question_id == question.id)
        .first()
        is not None
    )


def visible_questions_query():
    query = Question.query
    if not current_user.is_admin:
        query = query.filter(db.or_(Question.shared.is_(True), Question.owner_id == current_user.id))
    return query


@bp.route("/")
@staff_required
def index():
    q = request.args.get("q", "").strip()
    qtype = request.args.get("type", "")
    standard_id = parse_int(request.args.get("standard"))
    mine = parse_bool(request.args.get("mine", ""))
    archived = parse_bool(request.args.get("archived", ""))
    query = visible_questions_query().filter(Question.archived.is_(archived))
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(db.or_(db.func.lower(Question.stem).like(like), db.func.lower(Question.topic).like(like)))
    if qtype in QTYPES:
        query = query.filter(Question.qtype == qtype)
    if standard_id:
        query = query.filter(Question.standards.any(Standard.id == standard_id))
    if mine:
        query = query.filter(Question.owner_id == current_user.id)
    page = max(1, parse_int(request.args.get("page"), 1))
    pagination = query.order_by(Question.updated_at.desc()).paginate(page=page, per_page=40, error_out=False)
    standards = Standard.query.order_by(Standard.code).all()
    return render_template(
        "questions/index.html",
        pagination=pagination,
        standards=standards,
        filters={"q": q, "type": qtype, "standard": standard_id, "mine": mine, "archived": archived},
    )


def _apply_form(question, form):
    """Validate and copy the posted form onto ``question``. Returns a list of error strings."""
    errors = []
    qtype = form.get("qtype", question.qtype or "mc")
    if qtype not in QTYPES:
        return ["Unknown question type."]
    stem = form.get("stem", "").strip()
    if not stem:
        errors.append("The question text is required.")
    points = parse_float(form.get("points"), 1.0)
    if points is None or points <= 0:
        errors.append("Points must be a positive number.")
    in_use = question.id is not None and question_has_responses(question)
    if in_use and qtype != question.qtype:
        errors.append("The type of a question that already has student responses cannot be changed.")

    texts = form.getlist("choice_text")
    ids = form.getlist("choice_id")
    correct_marks = set(form.getlist("correct"))
    rows = []  # (existing_id or None, text, is_correct)
    if qtype in ("mc", "ms"):
        for idx, text in enumerate(texts):
            text = text.strip()
            if not text:
                continue
            existing = parse_int(ids[idx]) if idx < len(ids) else None
            rows.append((existing, text, str(idx) in correct_marks))
        if len(rows) < 2:
            errors.append("Provide at least two answer options.")
        n_correct = sum(1 for r in rows if r[2])
        if qtype == "mc" and n_correct != 1:
            errors.append("Mark exactly one option as correct.")
        if qtype == "ms" and n_correct < 1:
            errors.append("Mark at least one option as correct.")
    elif qtype == "tf":
        truth = form.get("tf_answer", "true") == "true"
        existing = {c.text: c.id for c in question.choices} if question.qtype == "tf" else {}
        rows = [(existing.get("True"), "True", truth), (existing.get("False"), "False", not truth)]
    elif qtype == "short":
        answers = [a.strip() for a in form.get("accepted_answers", "").splitlines() if a.strip()]
        if not answers:
            errors.append("Provide at least one accepted answer (one per line).")
        rows = [(None, a, True) for a in answers]
    numeric_answer = None
    if qtype == "numeric":
        numeric_answer = parse_float(form.get("numeric_answer"))
        if numeric_answer is None:
            errors.append("Provide the numeric answer.")
    tolerance = parse_float(form.get("numeric_tolerance"), 0.0) or 0.0
    if tolerance < 0:
        errors.append("Tolerance cannot be negative.")

    # Choices referenced by student answers must keep their identity.
    if qtype in CHOICE_TYPES and in_use:
        kept = {r[0] for r in rows if r[0]}
        if any(c.id not in kept for c in question.choices):
            errors.append("Options of a question that already has responses cannot be removed (you can still edit their text or the key).")

    if errors:
        return errors

    question.qtype = qtype
    question.stem = stem
    question.points = points
    question.explanation = form.get("explanation", "").strip()
    question.topic = form.get("topic", "").strip()[:120]
    question.shared = parse_bool(form.get("shared", ""))
    question.partial_credit = parse_bool(form.get("partial_credit", ""))
    question.case_sensitive = parse_bool(form.get("case_sensitive", ""))
    question.numeric_answer = numeric_answer
    question.numeric_tolerance = tolerance
    std_ids = [parse_int(s) for s in form.getlist("standards")]
    question.standards = Standard.query.filter(Standard.id.in_([s for s in std_ids if s])).all() if std_ids else []

    if qtype in ("mc", "ms", "tf", "short"):
        by_id = {c.id: c for c in question.choices}
        new_choices = []
        for position, (existing_id, text, is_correct) in enumerate(rows):
            choice = by_id.pop(existing_id, None) if existing_id else None
            if choice is None:
                choice = Choice()
            choice.text = text
            choice.is_correct = is_correct
            choice.position = position
            new_choices.append(choice)
        question.choices = new_choices
    else:
        question.choices = []
    return []


def _form_state(question, form, qtype_default="mc"):
    """Everything the question form needs, from a failed POST or from the stored question."""
    if form is not None and hasattr(form, "getlist"):
        texts, ids, marks = form.getlist("choice_text"), form.getlist("choice_id"), set(form.getlist("correct"))
        return {
            "qtype": form.get("qtype", qtype_default),
            "stem": form.get("stem", ""),
            "points": form.get("points", "1"),
            "explanation": form.get("explanation", ""),
            "topic": form.get("topic", ""),
            "shared": bool(form.get("shared")),
            "partial_credit": bool(form.get("partial_credit")),
            "case_sensitive": bool(form.get("case_sensitive")),
            "numeric_answer": form.get("numeric_answer", ""),
            "numeric_tolerance": form.get("numeric_tolerance", ""),
            "rows": [{"id": ids[i] if i < len(ids) else "", "text": t, "correct": str(i) in marks} for i, t in enumerate(texts)],
            "tf": form.get("tf_answer", "true"),
            "accepted": form.get("accepted_answers", ""),
            "standards": {parse_int(x) for x in form.getlist("standards")},
        }
    if question is not None:
        return {
            "qtype": question.qtype,
            "stem": question.stem,
            "points": f"{question.points:g}",
            "explanation": question.explanation,
            "topic": question.topic,
            "shared": question.shared,
            "partial_credit": question.partial_credit,
            "case_sensitive": question.case_sensitive,
            "numeric_answer": "" if question.numeric_answer is None else f"{question.numeric_answer:g}",
            "numeric_tolerance": f"{question.numeric_tolerance:g}" if question.numeric_tolerance else "",
            "rows": [{"id": c.id, "text": c.text, "correct": c.is_correct} for c in question.choices]
            if question.qtype in ("mc", "ms")
            else [],
            "tf": "true" if any(c.is_correct and c.text == "True" for c in question.choices) else "false",
            "accepted": "\n".join(c.text for c in question.choices) if question.qtype == "short" else "",
            "standards": {s.id for s in question.standards},
        }
    return {
        "qtype": qtype_default if qtype_default in QTYPES else "mc",
        "stem": "",
        "points": "1",
        "explanation": "",
        "topic": "",
        "shared": True,
        "partial_credit": True,
        "case_sensitive": False,
        "numeric_answer": "",
        "numeric_tolerance": "",
        "rows": [],
        "tf": "true",
        "accepted": "",
        "standards": set(),
    }


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:question_id>/edit", methods=["GET", "POST"])
@staff_required
def edit(question_id=None):
    question = get_question(question_id, edit=True) if question_id else None
    if question is not None and (question.topic or "").startswith("PDF: ") and question.items:
        flash("This question belongs to a test made from a PDF: change it on the answer key page.", "error")
        return redirect(url_for("pdftests.key", assessment_id=question.items[0].assessment_id))
    assessment = None
    assessment_id = parse_int(request.values.get("assessment_id"))
    if assessment_id:
        assessment = db.session.get(Assessment, assessment_id)
        if assessment is None or not can_manage_course(assessment.course):
            abort(403)
        if assessment.pdf is not None:  # its questions are the PDF's numbered questions: edit the answer key
            flash("This test comes from a PDF: change its questions on the answer key page.", "error")
            return redirect(url_for("pdftests.key", assessment_id=assessment.id))
    standards = Standard.query.filter_by(active=True).order_by(Standard.code).all()
    if question:
        standards = sorted(set(standards) | set(question.standards), key=lambda s: s.code)

    if request.method == "POST":
        is_new = question is None
        target = question or Question(owner=current_user, qtype=request.form.get("qtype", "mc"))
        errors = _apply_form(target, request.form)
        if errors:
            db.session.rollback()
            for e in errors:
                flash(e, "error")
            return render_template(
                "questions/form.html",
                question=question,
                state=_form_state(question, request.form),
                standards=standards,
                assessment=assessment,
                in_use=question is not None and question_has_responses(question),
            )
        if is_new:
            db.session.add(target)
        db.session.flush()
        regraded = 0
        if not is_new:
            for assessment_row in {item.assessment for item in target.items}:
                regraded += regrade_assessment(assessment_row)
        if assessment is not None and is_new:
            if assessment.is_locked:
                flash("The assessment already has attempts; the question was saved to the bank only.", "info")
            else:
                position = max([i.position for i in assessment.items], default=0) + 1
                db.session.add(AssessmentItem(assessment=assessment, question=target, position=position))
        log_event("question.saved", f"#{target.id}")
        db.session.commit()
        flash("Question saved." + (f" {regraded} submitted attempt(s) were re-scored." if regraded else ""), "success")
        if request.form.get("save_and_new"):
            return redirect(url_for("questions.edit", qtype=target.qtype, assessment_id=assessment.id if assessment else None))
        if assessment is not None:
            return redirect(url_for("assessments.detail", assessment_id=assessment.id))
        return redirect(url_for("questions.index"))

    return render_template(
        "questions/form.html",
        question=question,
        state=_form_state(question, None, request.args.get("qtype", "mc")),
        standards=standards,
        assessment=assessment,
        in_use=question is not None and question_has_responses(question),
    )


@bp.route("/<int:question_id>")
@staff_required
def preview(question_id):
    question = get_question(question_id)
    usage = [item.assessment for item in question.items]
    return render_template("questions/preview.html", question=question, usage=usage)


@bp.route("/<int:question_id>/duplicate", methods=["POST"])
@staff_required
def duplicate(question_id):
    source = get_question(question_id)
    copy = Question(
        owner=current_user,
        qtype=source.qtype,
        stem=source.stem,
        points=source.points,
        explanation=source.explanation,
        topic=source.topic,
        shared=source.shared,
        partial_credit=source.partial_credit,
        case_sensitive=source.case_sensitive,
        numeric_answer=source.numeric_answer,
        numeric_tolerance=source.numeric_tolerance,
        standards=list(source.standards),
        choices=[Choice(text=c.text, is_correct=c.is_correct, position=c.position) for c in source.choices],
    )
    db.session.add(copy)
    db.session.commit()
    flash("Question duplicated — you are now editing the copy.", "success")
    return redirect(url_for("questions.edit", question_id=copy.id))


@bp.route("/<int:question_id>/delete", methods=["POST"])
@staff_required
def delete(question_id):
    question = get_question(question_id, edit=True)
    if question.items:
        question.archived = not question.archived
        flash(
            "This question is used in assessments, so it was "
            + ("archived (hidden from the bank)." if question.archived else "restored."),
            "info",
        )
    else:
        db.session.delete(question)
        flash("Question deleted.", "success")
    log_event("question.deleted", f"#{question_id}")
    db.session.commit()
    return redirect(url_for("questions.index"))


# --------------------------------------------------------------------------- CSV import

TYPE_ALIASES = {
    "mc": "mc", "multiple choice": "mc", "multiple_choice": "mc", "single": "mc",
    "ms": "ms", "multiple select": "ms", "multiple_select": "ms", "multi": "ms", "checkbox": "ms",
    "tf": "tf", "true/false": "tf", "true false": "tf", "truefalse": "tf", "boolean": "tf",
    "short": "short", "short answer": "short", "short_answer": "short", "text": "short",
    "numeric": "numeric", "number": "numeric", "numerical": "numeric",
    "essay": "essay", "open": "essay", "constructed": "essay",
}


def _split(value):
    return [v.strip() for v in re.split(r"\s*\|\s*", value or "") if v.strip()]


def _correct_indexes(spec, options):
    """Accept 'B', 'A|C', 'AC', '2' or the option text itself."""
    spec = (spec or "").strip()
    picked = set()
    parts = _split(spec) if "|" in spec else ([spec] if not re.fullmatch(r"[A-Za-z]{2,}", spec) or spec in options else list(spec))
    for part in parts:
        if len(part) == 1 and part.isalpha() and ord(part.upper()) - 65 < len(options):
            picked.add(ord(part.upper()) - 65)
        elif part.isdigit() and 1 <= int(part) <= len(options):
            picked.add(int(part) - 1)
        elif part in options:
            picked.add(options.index(part))
    return picked


def build_question_from_row(row):
    qtype = TYPE_ALIASES.get((row.get("type") or "mc").strip().lower())
    stem = row.get("question") or row.get("stem") or ""
    if not qtype:
        raise ValueError(f"unknown type '{row.get('type')}'")
    if not stem:
        raise ValueError("missing question text")
    question = Question(
        owner=current_user,
        qtype=qtype,
        stem=stem.replace("\\n", "\n"),
        points=parse_float(row.get("points"), 1.0) or 1.0,
        explanation=row.get("explanation", ""),
        topic=row.get("topic", "")[:120],
        shared=not parse_bool(row.get("private", "")),
    )
    correct = row.get("correct") or row.get("answer") or ""
    if qtype in ("mc", "ms"):
        options = _split(row.get("choices") or row.get("options"))
        if len(options) < 2:
            raise ValueError("needs at least two choices separated by |")
        picked = _correct_indexes(correct, options)
        if not picked or (qtype == "mc" and len(picked) != 1):
            raise ValueError("could not determine the correct choice")
        question.choices = [Choice(text=o, is_correct=i in picked, position=i) for i, o in enumerate(options)]
    elif qtype == "tf":
        truth = correct.strip().lower() in ("true", "t", "yes", "1", "vrai", "a")
        question.choices = [Choice(text="True", is_correct=truth, position=0), Choice(text="False", is_correct=not truth, position=1)]
    elif qtype == "short":
        answers = _split(correct)
        if not answers:
            raise ValueError("missing accepted answer(s)")
        question.choices = [Choice(text=a, is_correct=True, position=i) for i, a in enumerate(answers)]
    elif qtype == "numeric":
        value = parse_float(correct)
        if value is None:
            raise ValueError("missing numeric answer")
        question.numeric_answer = value
        question.numeric_tolerance = abs(parse_float(row.get("tolerance"), 0.0) or 0.0)
    codes = re.split(r"[|;,]\s*", row.get("standards") or row.get("standard") or "")
    codes = [c.strip().lower() for c in codes if c.strip()]
    warning = None
    if codes:
        found = Standard.query.filter(db.func.lower(Standard.code).in_(codes)).all()
        question.standards = found
        missing = set(codes) - {s.code.lower() for s in found}
        if missing:
            warning = "unknown standard code(s): " + ", ".join(sorted(missing))
    return question, warning


@bp.route("/import", methods=["GET", "POST"])
@staff_required
def import_csv():
    if request.method == "POST":
        rows = read_csv_upload()
        if rows is None:
            return redirect(url_for("questions.import_csv"))
        created, errors = 0, []
        for lineno, row in enumerate(rows, start=2):
            try:
                with db.session.no_autoflush:
                    question, warning = build_question_from_row(row)
            except ValueError as exc:
                errors.append(f"Line {lineno}: {exc}")
                continue
            if warning:
                errors.append(f"Line {lineno}: {warning} (question imported without them)")
            db.session.add(question)
            created += 1
        log_event("question.import", f"created={created}")
        db.session.commit()
        flash(f"{created} question(s) imported.", "success")
        for err in errors[:25]:
            flash(err, "error")
        return redirect(url_for("questions.index", mine=1))
    return render_template("questions/import.html")
