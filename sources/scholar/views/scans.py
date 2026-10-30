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
    "multiple": "More than one bubble filled: pick one",
    "faint": "Light or unsure: is Scholar's guess right?",
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


def process_photo(assessment, data, filename="", auto=False):
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
                "quality": read.quality,
            }
        )
        flagged = any(v["flag"] for v in items.values())
        scan.status = "ready" if sheet.user_id and not flagged and not missing else "review"
        db.session.flush()
        # an older, unrecorded photo of the same page is superseded by this one
        for older in SheetScan.query.filter(
            SheetScan.sheet_id == sheet.id, SheetScan.status.in_(("ready", "review")), SheetScan.id != scan.id
        ):
            older.status = "replaced"
        if auto and scan.status == "ready":
            _auto_record(scan)
    except omr.OMRError as exc:
        scan.status = "failed"
        scan.error = str(exc)
        scan.image = _thumbnail(data)
    return scan


def _auto_record(scan):
    """Record a cleanly read sheet at once. A photo of a page that is already recorded with the same answers records nothing twice."""
    user = scan.sheet.user
    if user is None or not scan.assessment.course.has_student(user):
        scan.status = "review"
        return
    values = scan_values(scan)
    earlier = (
        SheetScan.query.filter(SheetScan.sheet_id == scan.sheet_id, SheetScan.status == "recorded", SheetScan.id != scan.id)
        .order_by(SheetScan.id.desc())
        .first()
    )
    if earlier is not None and scan_values(earlier) == values:
        scan.status = "replaced"
        scan.error = "duplicate"
        return
    try:
        record_scan(scan, user, values)
    except ValueError as exc:
        db.session.rollback()
        scan.status = "review"
        scan.error = str(exc)


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
    quality = result.get("quality", {})
    return {
        "id": scan.id,
        "status": scan.status,
        "duplicate": scan.error == "duplicate",
        "student": scan.user.display_name if scan.user else None,
        "page": result.get("page"),
        "pages": result.get("pages"),
        "flags": scan.flag_count,
        "answered": quality.get("answered"),
        "rows": quality.get("rows"),
        "blank": quality.get("blank", []),
        "light": bool(quality.get("mark_level") is not None and quality["mark_level"] < 0.3),
        "low_resolution": bool(quality.get("px_per_mm") is not None and quality["px_per_mm"] < omr.LOW_PX_PER_MM),
        "error": scan.error if scan.status != "replaced" else "",
        "filename": scan.filename,
        "url": url_for("scans.review", assessment_id=scan.assessment_id, scan_id=scan.id),
        "fix_url": url_for("scans.fix", assessment_id=scan.assessment_id, scan_id=scan.id),
    }


def class_progress(assessment):
    """Who has been scanned? Every student's printed pages against the photos taken so far."""
    pages = len(omr.plan_pages(assessment)) if assessment.items else 1
    covered = {}
    for scan in assessment.scans:
        if scan.status in ("failed", "replaced") or scan.user_id is None or scan.sheet is None:
            continue
        covered.setdefault(scan.user_id, {})[scan.sheet.page] = scan.status
    rows = []
    for student in assessment.course.students:
        mine = covered.get(student.id, {})
        if len(mine) >= pages:
            state = "recorded" if all(v == "recorded" for v in mine.values()) else "scanned"
        elif mine:
            state = "partial"
        else:
            state = "none"
        rows.append({"user": student, "state": state, "pages": len(mine)})
    done = sum(1 for r in rows if r["state"] in ("recorded", "scanned"))
    return {"rows": rows, "done": done, "total": len(rows), "pages": pages}


def progress_json(assessment):
    p = class_progress(assessment)
    return {"done": p["done"], "total": p["total"], "missing": [r["user"].display_name for r in p["rows"] if r["state"] != "recorded" and r["state"] != "scanned"]}


@bp.route("/<int:assessment_id>/scans")
@staff_required
def index(assessment_id):
    assessment = get_assessment(assessment_id)
    scans = sorted(assessment.scans, key=lambda s: -s.id)
    counts = {k: sum(1 for s in scans if s.status == k) for k in ("ready", "review", "recorded", "failed", "replaced")}
    return render_template(
        "assessments/scans.html",
        assessment=assessment,
        scans=scans,
        counts=counts,
        available=omr.available(),
        flag_text=FLAG_TEXT,
        progress=class_progress(assessment),
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
    auto = request.form.get("auto") in ("1", "true", "on")
    scans = [process_photo(assessment, f.read(), f.filename, auto=auto) for f in files]
    db.session.commit()
    if request.accept_mimetypes.best == "application/json":
        return jsonify({"scans": [_scan_json(s) for s in scans], "progress": progress_json(assessment)})
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
    """Positions (percent of the straightened image) of every marked or faint bubble, for the whole-sheet picture."""
    span_w = omr.FRAME_W + 2 * omr.MARGIN
    span_h = omr.FRAME_H + 2 * omr.MARGIN
    px = lambda v: round(100 * (v + omr.MARGIN) / span_w, 3)  # noqa: E731
    py = lambda v: round(100 * (v + omr.MARGIN) / span_h, 3)  # noqa: E731
    marks = []
    result = scan.get_result().get("items", {})
    for slot in scan.sheet.get_layout()["slots"]:
        read = result.get(str(slot["item"]), {})
        if slot["kind"] != "choice":
            continue
        states = read.get("states") or [""] * len(slot["bubbles"])
        for k, (x, y) in enumerate(slot["bubbles"]):
            if states[k]:
                marks.append({"x": px(x), "y": py(y), "d": round(100 * omr.BUBBLE_D / span_w, 3), "state": states[k], "flag": bool(read.get("flag"))})
    return marks


CROP_PX = 340  # width of one question's picture on screen


def _crop(slot, pad=1.4):
    """CSS offsets that show just one question's bubbles (or write-in box) out of the straightened photo."""
    span_w = omr.FRAME_W + 2 * omr.MARGIN
    if slot["kind"] == "choice":
        left, right = slot["x"] + 1.0, slot["bubbles"][-1][0] + omr.BUBBLE_D / 2 + pad
        top, height = slot["y"], omr.ROW_PITCH
    else:
        bx, by, bw, bh = slot["box"]
        left, right, top, height = bx - pad, bx + bw + pad, by - 1.0, bh + 2.0
    scale = CROP_PX / (right - left)
    return {
        "w": CROP_PX,
        "h": round(height * scale),
        "img_w": round(span_w * scale),
        "left": round(-(left + omr.MARGIN) * scale),
        "top": round(-(top + omr.MARGIN) * scale),
        "scale": scale,
        "x0": left,
        "y0": top,
    }


def build_rows(scan, assessment):
    """Every question of the page as one row: what was read, how sure, and a picture of it."""
    layout = scan.sheet.get_layout()
    result = scan.get_result().get("items", {})
    items = {i.id: i for i in assessment.items}
    rows = []
    for slot in layout["slots"]:
        read = result.get(str(slot["item"]), {})
        item = items.get(slot["item"])
        row = {"slot": slot, "item": item, "read": read, "flag": read.get("flag", ""), "value": read.get("value", ""), "crop": None, "bubbles": []}
        if item is not None and slot["kind"] in ("choice", "write"):
            row["crop"] = _crop(slot)
        if item is not None and slot["kind"] == "choice":
            crop, states = row["crop"], read.get("states") or [""] * len(slot["bubbles"])
            for k, (bx, by) in enumerate(slot["bubbles"]):
                size = omr.BUBBLE_D * crop["scale"]
                row["bubbles"].append(
                    {
                        "label": slot["labels"][k],
                        "left": round((bx - crop["x0"]) * crop["scale"] - size / 2),
                        "top": round((by - crop["y0"]) * crop["scale"] - size / 2),
                        "size": round(size),
                        "state": states[k],
                    }
                )
        rows.append(row)
    return rows


def submit_scan(assessment, scan, form):
    """Record a scan with the answers in ``form``. Returns (ok, message, student)."""
    user = db.session.get(User, parse_int(form.get("user_id"), 0))
    if user is None or not assessment.course.has_student(user):
        return False, "Choose which student this sheet belongs to.", None
    try:
        record_scan(scan, user, scan_values(scan, form))
    except ValueError as exc:
        db.session.rollback()
        return False, f"Not recorded: {exc}", None
    db.session.commit()
    return True, f"Recorded page {scan.get_result().get('page')} for {user.display_name}.", user


def _next_to_check(assessment, after_id=None):
    waiting = [s for s in sorted(assessment.scans, key=lambda s: s.id) if s.status == "review" and s.id != after_id]
    return waiting[0] if waiting else None


@bp.route("/<int:assessment_id>/scans/<int:scan_id>", methods=["GET", "POST"])
@staff_required
def review(assessment_id, scan_id):
    assessment = get_assessment(assessment_id)
    scan = get_scan(assessment, scan_id)
    if scan.status == "failed" or scan.sheet is None:
        return render_template("assessments/scan_failed.html", assessment=assessment, scan=scan)
    if request.method == "POST":
        ok, message, user = submit_scan(assessment, scan, request.form)
        flash(message, "success" if ok else "error")
        if ok:
            nxt = _next_to_check(assessment)
            if nxt is not None:
                return redirect(url_for("scans.review", assessment_id=assessment.id, scan_id=nxt.id))
            return redirect(url_for("scans.index", assessment_id=assessment.id))
    rows = build_rows(scan, assessment)
    return render_template(
        "assessments/scan_review.html",
        assessment=assessment,
        scan=scan,
        rows=rows,
        marks=_overlay(scan),
        students=assessment.course.students,
        result=scan.get_result(),
        flag_text=FLAG_TEXT,
        form_values=request.form if request.method == "POST" else None,
    )


@bp.route("/<int:assessment_id>/scans/<int:scan_id>/fix", methods=["GET", "POST"])
@staff_required
def fix(assessment_id, scan_id):
    """The live scanner's quick fix: only the answers Scholar was unsure about, tapped on the photo."""
    assessment = get_assessment(assessment_id)
    scan = get_scan(assessment, scan_id)
    if scan.status == "failed" or scan.sheet is None:
        abort(404)
    if request.method == "POST":
        ok, message, user = submit_scan(assessment, scan, request.form)
        return jsonify({"ok": ok, "message": message, "scan": _scan_json(scan), "progress": progress_json(assessment)}), (200 if ok else 400)
    return render_template(
        "assessments/_scan_fix.html",
        assessment=assessment,
        scan=scan,
        rows=build_rows(scan, assessment),
        students=assessment.course.students,
        result=scan.get_result(),
        flag_text=FLAG_TEXT,
        form_values=None,
        panel=True,
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
