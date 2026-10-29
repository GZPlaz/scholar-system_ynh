from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from ..extensions import db
from ..models import Standard
from ..utils import log_event, parse_bool, read_csv_upload, staff_required

bp = Blueprint("standards", __name__, url_prefix="/standards")


@bp.route("/")
@staff_required
def index():
    q = request.args.get("q", "").strip()
    subject = request.args.get("subject", "")
    query = Standard.query
    if q:
        like = f"%{q.lower()}%"
        query = query.filter(db.or_(db.func.lower(Standard.code).like(like), db.func.lower(Standard.title).like(like)))
    if subject:
        query = query.filter(Standard.subject == subject)
    standards = query.order_by(Standard.subject, Standard.code).all()
    subjects = [s for (s,) in db.session.query(Standard.subject).distinct().order_by(Standard.subject) if s]
    return render_template("standards/index.html", standards=standards, q=q, subject=subject, subjects=subjects)


@bp.route("/new", methods=["GET", "POST"])
@bp.route("/<int:standard_id>/edit", methods=["GET", "POST"])
@staff_required
def edit(standard_id=None):
    standard = db.session.get(Standard, standard_id) if standard_id else None
    if standard_id and standard is None:
        abort(404)
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        title = request.form.get("title", "").strip()
        clash = Standard.query.filter(db.func.lower(Standard.code) == code.lower()).first()
        if not code or not title:
            flash("Code and title are required.", "error")
        elif clash and clash is not standard:
            flash(f"A standard with code '{code}' already exists.", "error")
        else:
            if standard is None:
                standard = Standard()
                db.session.add(standard)
            standard.code = code[:60]
            standard.title = title[:300]
            standard.description = request.form.get("description", "").strip()
            standard.subject = request.form.get("subject", "").strip()[:120]
            standard.active = parse_bool(request.form.get("active", ""))
            log_event("standard.saved", standard.code)
            db.session.commit()
            flash("Standard saved.", "success")
            return redirect(url_for("standards.index"))
        return render_template("standards/form.html", standard=standard, form=request.form)
    return render_template("standards/form.html", standard=standard, form={})


@bp.route("/<int:standard_id>/delete", methods=["POST"])
@staff_required
def delete(standard_id):
    standard = db.session.get(Standard, standard_id) or abort(404)
    if standard.questions:
        standard.active = False
        flash("This standard is aligned to questions, so it was deactivated instead of deleted.", "info")
    else:
        db.session.delete(standard)
        flash("Standard deleted.", "success")
    log_event("standard.deleted", standard.code)
    db.session.commit()
    return redirect(url_for("standards.index"))


@bp.route("/import", methods=["GET", "POST"])
@staff_required
def import_csv():
    if request.method == "POST":
        rows = read_csv_upload()
        if rows is None:
            return redirect(url_for("standards.import_csv"))
        created = updated = 0
        for row in rows:
            code = row.get("code", "")[:60]
            title = (row.get("title") or row.get("name") or "")[:300]
            if not code or not title:
                continue
            standard = Standard.query.filter(db.func.lower(Standard.code) == code.lower()).first()
            if standard is None:
                standard = Standard(code=code)
                db.session.add(standard)
                created += 1
            else:
                updated += 1
            standard.title = title
            standard.description = row.get("description", standard.description or "")
            standard.subject = (row.get("subject") or standard.subject or "")[:120]
            db.session.flush()
        log_event("standard.import", f"created={created} updated={updated}")
        db.session.commit()
        flash(f"{created} standard(s) created, {updated} updated.", "success")
        return redirect(url_for("standards.index"))
    return render_template("standards/import.html")
