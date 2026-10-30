"""Scanning photographed answer sheets, end to end through the web endpoints."""
import io
import json

import pytest

cv2 = pytest.importorskip("cv2")

from test_app import app, login  # noqa: E402,F401  (fixture re-export; also puts sources/ on sys.path)
from omr_synth import fake_photo, render_sheet  # noqa: E402

from scholar import omr  # noqa: E402
from scholar.extensions import db  # noqa: E402
from scholar.models import AnswerSheet, Assessment, AssessmentItem, Attempt, Choice, Course, Question, SheetScan, User  # noqa: E402


def demo(app_):
    with app_.app_context():
        a = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        return a.id, a.course_id


def correct_marks(layout, assessment, write=True):
    items = {i.id: i for i in assessment.items}
    marks = {}
    for slot in layout["slots"]:
        q = items[slot["item"]].question
        if slot["kind"] == "choice":
            marks[slot["item"]] = [k for k, c in enumerate(q.choices) if c.is_correct]
        elif slot["kind"] == "write" and write:
            marks[slot["item"]] = []
    return marks


def upload(client, aid, photo, name="sheet.jpg"):
    r = client.post(
        f"/staff/assessments/{aid}/scans/upload",
        data={"photos": (io.BytesIO(photo), name)},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, r.data[:300]
    return r.json["scans"][0]


def test_print_creates_one_sheet_per_student(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid, _ = demo(app)
    r = client.get(f"/staff/assessments/{aid}/bubble-sheets")
    assert r.status_code == 200 and r.data.count(b'class="omr-sheet"') == 8
    client.get(f"/staff/assessments/{aid}/bubble-sheets")  # printing again reuses the same sheet ids
    with app.app_context():
        assert AnswerSheet.query.filter_by(assessment_id=aid).count() == 8


def test_scan_with_write_ins_goes_to_review_then_records(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid, _ = demo(app)
    client.get(f"/staff/assessments/{aid}/bubble-sheets")
    with app.app_context():
        student = User.query.filter_by(username="demo.student8").one()
        sheet = AnswerSheet.query.filter_by(assessment_id=aid, user_id=student.id).one()
        layout = sheet.get_layout()
        photo = fake_photo(render_sheet(layout, sheet.id, correct_marks(layout, db.session.get(Assessment, aid))), rotation=1, seed=3)
        sid = student.id
    scan = upload(client, aid, photo)
    assert scan["status"] == "review" and scan["student"] == "Lucas Silva" and scan["flags"] == 2  # the two written answers
    r = client.get(scan["url"])
    assert r.status_code == 200 and b"Written answer: type it in" in r.data and b"scan-crop" in r.data
    with app.app_context():
        result = db.session.get(SheetScan, scan["id"]).get_result()["items"]
        a = db.session.get(Assessment, aid)
        form = {"user_id": sid}
        for item in a.items:
            read = result[str(item.id)]
            form[f"q_{item.id}"] = {"short": "zero", "numeric": "5", "essay": "3"}.get(item.question.qtype, read["value"])
    r = client.post(scan["url"], data=form, follow_redirects=True)
    assert b"Recorded page 1 for Lucas Silva" in r.data
    with app.app_context():
        attempt = Attempt.query.filter_by(assessment_id=aid, user_id=sid, source="paper").one()
        assert attempt.score == 12.0 and attempt.max_score == 13.0 and not attempt.needs_grading
        assert db.session.get(SheetScan, scan["id"]).status == "recorded"


def test_blank_sheet_needs_a_student_and_garbage_fails(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid, _ = demo(app)
    client.get(f"/staff/assessments/{aid}/bubble-sheets?blank=1")
    with app.app_context():
        sheet = AnswerSheet.query.filter_by(assessment_id=aid, user_id=None).one()
        layout = sheet.get_layout()
        photo = fake_photo(render_sheet(layout, sheet.id, correct_marks(layout, db.session.get(Assessment, aid), write=False)), rotation=2, seed=5)
    scan = upload(client, aid, photo)
    assert scan["status"] == "review" and scan["student"] is None
    r = client.post(scan["url"], data={"user_id": ""}, follow_redirects=True)
    assert b"Choose which student" in r.data
    bad = upload(client, aid, b"not an image at all", "notes.txt")
    assert bad["status"] == "failed" and "not an image" in bad["error"]
    import numpy as np

    blank_page = cv2.imencode(".jpg", np.full((400, 300), 255, np.uint8))[1].tobytes()
    assert upload(client, aid, blank_page)["status"] == "failed"


def test_sheet_from_another_assessment_is_rejected(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid, cid = demo(app)
    r = client.post("/staff/assessments/new", data={"course_id": cid, "title": "Quiz 9", "max_attempts": "1", "show_results": "immediately", "allow_online": "1"})
    other = int(r.headers["Location"].rstrip("/").split("/")[-1])
    client.get(f"/staff/assessments/{aid}/bubble-sheets")
    with app.app_context():
        sheet = AnswerSheet.query.filter_by(assessment_id=aid).first()
        photo = fake_photo(render_sheet(sheet.get_layout(), sheet.id, {}), seed=9)
    scan = upload(client, other, photo)
    assert scan["status"] == "failed" and "Unit 1 Check-in" in scan["error"]


def test_multi_page_sheets_merge_into_one_attempt(app):
    """130 choice questions don't fit one page: two pages per student, recorded in one go."""
    with app.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        prof = User.query.filter_by(username="demo.instructor").one()
        a = Assessment(course=course, owner=prof, title="Long final", status="published")
        db.session.add(a)
        for n in range(130):
            q = Question(owner=prof, qtype="mc", stem=f"Q{n}", points=1.0, choices=[Choice(text=t, is_correct=(i == n % 4), position=i) for i, t in enumerate("wxyz")])
            db.session.add(q)
            a.items.append(AssessmentItem(question=q, position=n + 1))
        db.session.commit()
        aid = a.id
        assert len(omr.plan_pages(a)) == 2
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    r = client.get(f"/staff/assessments/{aid}/bubble-sheets")
    assert r.data.count(b'class="omr-sheet"') == 16
    with app.app_context():
        student = User.query.filter_by(username="demo.student2").one()
        a = db.session.get(Assessment, aid)
        photos = []
        for sheet in AnswerSheet.query.filter_by(assessment_id=aid, user_id=student.id).order_by(AnswerSheet.page):
            layout = sheet.get_layout()
            marks = correct_marks(layout, a)
            first = layout["slots"][0]["item"]
            marks[first] = [(marks[first][0] + 1) % 4]  # one wrong answer per page
            photos.append(fake_photo(render_sheet(layout, sheet.id, marks), rotation=sheet.page, seed=sheet.page))
        sid = student.id
    statuses = [upload(client, aid, p)["status"] for p in reversed(photos)]  # order doesn't matter
    assert statuses == ["ready", "ready"]
    r = client.post(f"/staff/assessments/{aid}/scans/record-ready", follow_redirects=True)
    assert b"Recorded 2 sheet(s)" in r.data
    with app.app_context():
        attempt = Attempt.query.filter_by(assessment_id=aid, user_id=sid, source="paper").one()
        assert len(attempt.responses) == 130 and attempt.score == 128.0


def test_rescanning_a_page_replaces_the_unrecorded_photo(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid, _ = demo(app)
    client.get(f"/staff/assessments/{aid}/bubble-sheets")
    with app.app_context():
        sheet = AnswerSheet.query.filter_by(assessment_id=aid).first()
        photo = fake_photo(render_sheet(sheet.get_layout(), sheet.id, {}), seed=1)
    first = upload(client, aid, photo)
    second = upload(client, aid, photo)
    with app.app_context():
        assert db.session.get(SheetScan, first["id"]).status == "replaced"
        assert db.session.get(SheetScan, second["id"]).status in ("ready", "review")
    assert client.get(f"/staff/assessments/{aid}/scans").status_code == 200
    assert client.get(f"/staff/assessments/{aid}/scans/{second['id']}/image").mimetype == "image/jpeg"


def test_sheet_code_round_trip():
    for sheet_id in (1, 255, 4242, (1 << 24) - 1):
        assert omr.decode_bits(omr.encode_sheet_id(sheet_id)) == sheet_id
    bits = omr.encode_sheet_id(4242)
    bits[10] ^= 1
    assert omr.decode_bits(bits) is None
    assert json.dumps(omr.encode_sheet_id(7))
