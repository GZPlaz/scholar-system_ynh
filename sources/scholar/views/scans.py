"""Photographed answer sheets: print sheets, upload photos, review what was read, record it."""
import json

from flask import Blueprint, Response, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from .. import omr
from ..extensions import db
from ..models import AnswerSheet, SheetScan, User, utcnow
from ..utils import log_event, parse_int, staff_required
from .assessments import get_assessment, record_paper_attempt

bp = Blueprint("scans", __name__, url_prefix="/assessments")

FLAG_TEXT = {
    "multiple": "More than one bubble filled",
    "faint": "Light or partly erased mark",
    "write": "Written answer: type it in",
}


def sheets_for(assessment, students):
    """Get or create the AnswerSheet rows to print for these students (None = blank sheet)."""
    layouts = omr.plan_pages(assessment)
    out = []
    for user in students:
        for layout in layouts:
            digest = omr.layout_hash(layout)
            sheet = AnswerSheet.query.filter_by(
                assessment_id=assessment.id, user_id=user.id if user else None, page=layout["page"], layout_hash=digest
            ).first()
            if sheet is None:
                sheet = AnswerSheet(
                    assessment_id=assessment.id,
                    user_id=user.id if user else None,
                    page=layout["page"],
                    pages=layout["pages"],
                    layout=json.dumps(layout),
                    layout_hash=digest,
                )
                db.session.add(sheet)
                db.session.flush()
            out.append(
                {
                    "user": user,
                    "sheet": sheet,
                    "layout": layout,
                    "code": [cell for bit, cell in zip(omr.encode_sheet_id(sheet.id), omr.code_cells()) if bit],
                }
            )
    return out


def _thumbnail(data):
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        scale = 900.0 / max(img.shape)
        if scale < 1:
            img = cv2.resize(img, (int(img.shape[1] * scale), int(img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        return buf.tobytes() if ok else None
    except ImportError:  # pragma: no cover
        return None


def process_photo(assessment, data, filename=""):
    scan = SheetScan(assessment_id=assessment.id, uploaded_by_id=current_user.id, filename=(filename or "")[:255])
    db.session.add(scan)
    try:
        read = omr.read_photo(data)
        sheet = db.session.get(AnswerSheet, read.sheet_id)
        if sheet is None:
            raise omr.OMRError(f"Sheet number {read.sheet_id} was not printed by this Scholar server.")
        if sheet.assessment_id != assessment.id:
            raise omr.OMRError(f"This is an answer sheet for “{sheet.assessment.title}” ({sheet.assessment.course.code}), not for this assessment.")
        layout = sheet.get_layout()
        items = omr.read_answers(read, layout)
        current = {i.id for i in assessment.items}
        missing = [slot["n"] for slot in layout["slots"] if slot["item"] not in current]
        scan.sheet = sheet
        scan.user_id = sheet.user_id
        scan.image = read.jpeg()
        scan.result = json.dumps(
            {
                "items": {str(k): v for k, v in items.items()},
                "page": layout["page"],
                "pages": layout["pages"],
                "removed": missing,
            }
        )
        flagged = any(v["flag"] for v in items.values())
        scan.status = "ready" if sheet.user_id and not flagged and not missing else "review"
        # an older, unrecorded photo of the same page is superseded by this one
        for older in SheetScan.query.filter(
            SheetScan.sheet_id == sheet.id, SheetScan.status.in_(("ready", "review")), SheetScan.id != scan.id
        ):
            if older is not scan:
                older.status = "replaced"
    except omr.OMRError as exc:
        scan.status = "failed"
        scan.error = str(exc)
        scan.image = _thumbnail(data)
    return scan


def scan_values(scan, overrides=None):
    """The answers to record for a scan: {item_id: raw}, from what was read or the review form."""
    result = scan.get_result()
    layout = scan.sheet.get_layout()
    current = {i.id for i in scan.assessment.items}
    values = {}
    for slot in layout["slots"]:
        if slot["item"] not in current:
            continue
        read = result.get("items", {}).get(str(slot["item"]), {})
        values[slot["item"]] = read.get("value", "")
        if overrides is not None:
            values[slot["item"]] = (overrides.get(f"q_{slot['item']}") or "").strip()
    return values


def record_scan(scan, user, values):
    record_paper_attempt(scan.assessment, user, values, partial=True)
    scan.user_id = user.id
    scan.status = "recorded"
    scan.recorded_at = utcnow()
    log_event("scan.recorded", f"scan #{scan.id} assessment #{scan.assessment_id} {user.username}")


def get_scan(assessment, scan_id):
    scan = db.session.get(SheetScan, scan_id)
    if scan is None or scan.assessment_id != assessment.id:
        abort(404)
    return scan


def _scan_json(scan):
    result = scan.get_result()
    return {
        "id": scan.id,
        "status": scan.status,
        "student": scan.user.display_name if scan.user else None,
        "page": result.get("page"),
        "pages": result.get("pages"),
        "flags": scan.flag_count,
        "error": scan.error,
        "filename": scan.filename,
        "url": url_for("scans.review", assessment_id=scan.assessment_id, scan_id=scan.id),
    }


@bp.route("/<int:assessment_id>/scans")
@staff_required
def index(assessment_id):
    assessment = get_assessment(assessment_id)
    scans = sorted(assessment.scans, key=lambda s: -s.id)
    counts = {k: sum(1 for s in scans if s.status == k) for k in ("ready", "review", "recorded", "failed", "replaced")}
    return render_template(
        "assessments/scans.html", assessment=assessment, scans=scans, counts=counts, available=omr.available(), flag_text=FLAG_TEXT
    )


@bp.route("/<int:assessment_id>/scans/upload", methods=["POST"])
@staff_required
def upload(assessment_id):
    assessment = get_assessment(assessment_id)
    files = [f for f in request.files.getlist("photos") if f and f.filename]
    if not files:
        if request.accept_mimetypes.best == "application/json":
            return jsonify({"error": "No photo received."}), 400
        flash("Choose or take at least one photo.", "error")
        return redirect(url_for("scans.index", assessment_id=assessment.id))
    scans = [process_photo(assessment, f.read(), f.filename) for f in files]
    db.session.commit()
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"scans": [_scan_json(s) for s in scans]})
    ok = sum(1 for s in scans if s.status != "failed")
    flash(f"{ok} of {len(scans)} photo(s) read.", "success" if ok == len(scans) else "info")
    return redirect(url_for("scans.index", assessment_id=assessment.id))


@bp.route("/<int:assessment_id>/scans/<int:scan_id>/image")
@staff_required
def image(assessment_id, scan_id):
    scan = get_scan(get_assessment(assessment_id), scan_id)
    if not scan.image:
        abort(404)
    return Response(scan.image, mimetype="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


def _overlay(scan):
    """Positions (percent of the straightened image) of every bubble and write-in box, with what was read."""
    span_w = omr.FRAME_W + 2 * omr.MARGIN
    span_h = omr.FRAME_H + 2 * omr.MARGIN
    px = lambda v: round(100 * (v + omr.MARGIN) / span_w, 3)  # noqa: E731
    py = lambda v: round(100 * (v + omr.MARGIN) / span_h, 3)  # noqa: E731
    marks, boxes = [], []
    result = scan.get_result().get("items", {})
    for slot in scan.sheet.get_layout()["slots"]:
        read = result.get(str(slot["item"]), {})
        if slot["kind"] == "choice":
            for k, (x, y) in enumerate(slot["bubbles"]):
                fill = (read.get("fills") or [0] * len(slot["bubbles"]))[k]
                state = "marked" if fill >= omr.MARK_LEVEL else ("faint" if fill >= omr.FAINT_LEVEL else "")
                if state:
                    marks.append({"x": px(x), "y": py(y), "d": round(100 * omr.BUBBLE_D / span_w, 3), "state": state, "flag": bool(read.get("flag"))})
        elif slot["kind"] == "write":
            x, y, w, h = slot["box"]
            boxes.append({"item": slot["item"], "x": x, "y": y, "w": w, "h": h})
    return marks, boxes


@bp.route("/<int:assessment_id>/scans/<int:scan_id>", methods=["GET", "POST"])
@staff_required
def review(assessment_id, scan_id):
    assessment = get_assessment(assessment_id)
    scan = get_scan(assessment, scan_id)
    students = assessment.course.students
    if scan.status == "failed" or scan.sheet is None:
        return render_template("assessments/scan_failed.html", assessment=assessment, scan=scan)
    if request.method == "POST":
        user = db.session.get(User, parse_int(request.form.get("user_id"), 0))
        if user is None or not assessment.course.has_student(user):
            flash("Choose which student this sheet belongs to.", "error")
        else:
            try:
                record_scan(scan, user, scan_values(scan, request.form))
            except ValueError as exc:
                db.session.rollback()
                flash(f"Not recorded: {exc}", "error")
            else:
                db.session.commit()
                flash(f"Recorded page {scan.get_result().get('page')} for {user.display_name}.", "success")
                nxt = next((s for s in sorted(assessment.scans, key=lambda s: s.id) if s.status == "review"), None)
                if nxt is not None:
                    return redirect(url_for("scans.review", assessment_id=assessment.id, scan_id=nxt.id))
                return redirect(url_for("scans.index", assessment_id=assessment.id))
    layout = scan.sheet.get_layout()
    result = scan.get_result()
    items = {i.id: i for i in assessment.items}
    rows = []
    for slot in layout["slots"]:
        read = result.get("items", {}).get(str(slot["item"]), {})
        rows.append({"slot": slot, "item": items.get(slot["item"]), "read": read})
    marks, boxes = _overlay(scan)
    span_w = omr.FRAME_W + 2 * omr.MARGIN
    crop_w = 300  # px on screen
    crops = {}
    for b in boxes:
        scale = crop_w / (b["w"] + 4)
        crops[b["item"]] = {
            "w": crop_w,
            "h": round((b["h"] + 2) * scale),
            "img_w": round(span_w * scale),
            "left": round(-(b["x"] - 2 + omr.MARGIN) * scale),
            "top": round(-(b["y"] - 1 + omr.MARGIN) * scale),
        }
    form_values = request.form if request.method == "POST" else None
    return render_template(
        "assessments/scan_review.html",
        assessment=assessment,
        scan=scan,
        rows=rows,
        marks=marks,
        crops=crops,
        students=students,
        result=result,
        flag_text=FLAG_TEXT,
        form_values=form_values,
    )


@bp.route("/<int:assessment_id>/scans/record-ready", methods=["POST"])
@staff_required
def record_ready(assessment_id):
    assessment = get_assessment(assessment_id)
    done = 0
    for scan in [s for s in assessment.scans if s.status == "ready"]:
        user = scan.sheet.user if scan.sheet else None
        if user is None or not assessment.course.has_student(user):
            scan.status = "review"
            continue
        try:
            record_scan(scan, user, scan_values(scan))
            done += 1
        except ValueError as exc:
            scan.status = "review"
            scan.error = str(exc)
    db.session.commit()
    waiting = sum(1 for s in assessment.scans if s.status == "review")
    flash(f"Recorded {done} sheet(s)." + (f" {waiting} still need a look." if waiting else ""), "success")
    return redirect(url_for("scans.index", assessment_id=assessment.id))


@bp.route("/<int:assessment_id>/scans/<int:scan_id>/delete", methods=["POST"])
@staff_required
def delete(assessment_id, scan_id):
    assessment = get_assessment(assessment_id)
    scan = get_scan(assessment, scan_id)
    db.session.delete(scan)
    db.session.commit()
    flash("Photo removed. Answers already recorded from it are kept.", "success")
    return redirect(url_for("scans.index", assessment_id=assessment.id))
