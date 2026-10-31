"""Tests made from a PDF: upload the test, type in the correct answers."""
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.datastructures import MultiDict

from .. import pdftest
from ..extensions import db
from ..grading import regrade_assessment
from ..models import Assessment, Course, PdfPage
from ..security import ensure_code
from ..utils import log_event, parse_int, require_course_manager, staff_required
from .assessments import _apply_settings, get_assessment

bp = Blueprint("pdftests", __name__, url_prefix="/assessments")

DEFAULT_SETTINGS = {
    "max_attempts": "1",
    "show_results": "immediately",
    "allow_online": "1",
    "identity_check": "confirm",
    "lock_to_device": "1",
    "block_copy_paste": "1",
    "watermark": "1",
    "password_rotation_minutes": "5",
    "focus_grace_seconds": "0",
}


def _read_pdf(field="pdf"):
    upload = request.files.get(field)
    if upload is None or not upload.filename:
        raise pdftest.PdfProblem("Choose the test as a PDF file.")
    data = upload.read()
    if not data:
        raise pdftest.PdfProblem("That file is empty.")
    return upload.filename, data


@bp.route("/new-from-pdf", methods=["GET", "POST"])
@staff_required
def new_from_pdf():
    course = db.session.get(Course, parse_int(request.values.get("course_id"), 0)) or abort(404)
    require_course_manager(course)
    available = pdftest.available()
    if request.method == "POST" and available:
        try:
            filename, data = _read_pdf()
            title = request.form.get("title", "").strip() or filename.rsplit(".", 1)[0]
            assessment = Assessment()
            errors = _apply_settings(assessment, MultiDict({**DEFAULT_SETTINGS, "title": title}))
            if errors:
                raise pdftest.PdfProblem(errors[0])
            assessment.course = course
            assessment.owner = current_user
            db.session.add(assessment)
            pages = pdftest.attach_pdf(assessment, filename, data)
        except pdftest.PdfProblem as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            log_event("assessment.created", f"{course.code}: {assessment.title} (from a PDF, {pages} page(s))")
            db.session.commit()
            count = max(1, min(pdftest.MAX_QUESTIONS, parse_int(request.form.get("questions"), 20)))
            choices = max(2, min(8, parse_int(request.form.get("choices"), 4)))
            flash(f"The PDF was added ({pages} page{'s' if pages != 1 else ''}). Now enter the correct answers.", "success")
            return redirect(url_for("pdftests.key", assessment_id=assessment.id, rows=count, choices=choices))
    return render_template("assessments/pdf_new.html", course=course, available=available, form=request.form)


@bp.route("/<int:assessment_id>/key", methods=["GET", "POST"])
@staff_required
def key(assessment_id):
    assessment = get_assessment(assessment_id)
    if assessment.pdf is None:
        abort(404)
    rows, errors = None, []
    if request.method == "POST":
        rows, errors = pdftest.parse_rows(request.form)
        if not rows and not errors:
            errors.append("Enter the correct answer to at least one question.")
        if not errors:
            try:
                count = pdftest.save_key(assessment, rows, current_user)
                regraded = regrade_assessment(assessment) if assessment.attempts else 0
            except pdftest.KeyProblem as exc:
                db.session.rollback()
                errors.append(str(exc))
            else:
                log_event("assessment.key", f"#{assessment.id}: {count} question(s)")
                db.session.commit()
                points = sum(i.effective_points for i in assessment.items)
                flash(f"Answer key saved: {count} question{'s' if count != 1 else ''}, {points:g} points." + (f" {regraded} attempt(s) re-scored." if regraded else ""), "success")
                return redirect(url_for("pdftests.key", assessment_id=assessment.id))
        for message in errors[:8]:
            flash(message, "error")
        if len(errors) > 8:
            flash(f"…and {len(errors) - 8} more.", "error")
    if rows is None:
        rows = pdftest.key_rows(assessment)
        if not rows:  # a new test: empty rows to fill in
            count = max(1, min(pdftest.MAX_QUESTIONS, parse_int(request.args.get("rows"), 20)))
            rows = pdftest.blank_rows(count, choices=max(2, min(8, parse_int(request.args.get("choices"), 4))))
    return render_template(
        "assessments/pdf_key.html",
        assessment=assessment,
        rows=rows,
        locked=assessment.is_locked,
        key_types=pdftest.KEY_TYPES,
        saved=bool(assessment.items),
    )


@bp.route("/<int:assessment_id>/pdf")
@staff_required
def download(assessment_id):
    assessment = get_assessment(assessment_id)
    if assessment.pdf is None:
        abort(404)
    name = assessment.pdf.filename.replace('"', "")
    return Response(
        bytes(assessment.pdf.data),
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "private, no-store"},
    )


@bp.route("/<int:assessment_id>/pdf/page/<int:number>.jpg")
@staff_required
def page(assessment_id, number):
    assessment = get_assessment(assessment_id)
    row = PdfPage.query.filter_by(assessment_id=assessment.id, page=number).first() or abort(404)
    return Response(bytes(row.data), mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=600"})


@bp.route("/<int:assessment_id>/pdf/replace", methods=["POST"])
@staff_required
def replace(assessment_id):
    assessment = get_assessment(assessment_id)
    if assessment.pdf is None:
        abort(404)
    try:
        filename, data = _read_pdf()
        pages = pdftest.attach_pdf(assessment, filename, data)
    except pdftest.PdfProblem as exc:
        db.session.rollback()
        flash(str(exc), "error")
    else:
        log_event("assessment.pdf_replaced", f"#{assessment.id}: {filename}")
        db.session.commit()
        flash(f"The PDF was replaced ({pages} page{'s' if pages != 1 else ''}). Your answer key is unchanged: check that the question numbers still match.", "success")
    return redirect(url_for("pdftests.key", assessment_id=assessment.id))
