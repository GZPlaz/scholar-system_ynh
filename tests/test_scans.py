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
    assert r.status_code == 200 and b"Written answer: type it in" in r.data and b"fix-crop" in r.data
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


# --------------------------------------------------------------------------- the improved scanner


def choice_only_assessment(app_, title="Quick quiz", count=6):
    """A quiz of choice questions only, so a clean sheet needs no typing."""
    with app_.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        prof = User.query.filter_by(username="demo.instructor").one()
        a = Assessment(course=course, owner=prof, title=title, status="published")
        db.session.add(a)
        pool = [q for q in Question.query.filter(Question.qtype.in_(("mc", "tf"))).order_by(Question.id)][:count]
        for n, q in enumerate(pool, start=1):
            a.items.append(AssessmentItem(question=q, position=n))
        db.session.commit()
        return a.id


def photo_of(app_, aid, username, rotation=0, seed=1, mutate=None):
    with app_.app_context():
        student = User.query.filter_by(username=username).one()
        a = db.session.get(Assessment, aid)
        sheet = AnswerSheet.query.filter_by(assessment_id=aid, user_id=student.id).one()
        layout = sheet.get_layout()
        marks = correct_marks(layout, a)
        if mutate:
            mutate(layout, marks)
        return fake_photo(render_sheet(layout, sheet.id, marks), rotation=rotation, seed=seed)


def upload_auto(client, aid, photo):
    r = client.post(
        f"/staff/assessments/{aid}/scans/upload",
        data={"photos": (io.BytesIO(photo), "s.jpg"), "auto": "1"},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 200, r.data[:300]
    return r.json


def test_clear_sheets_record_themselves_and_duplicates_are_noticed(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid = choice_only_assessment(app)
    client.get(f"/staff/assessments/{aid}/bubble-sheets")
    photo = photo_of(app, aid, "demo.student3", rotation=2, seed=4)
    # without the option the clean sheet waits to be recorded
    waiting = upload(client, aid, photo)
    assert waiting["status"] == "ready" and waiting["answered"] == 6 and waiting["blank"] == []
    # with it, the next photo of the same page records it, and a second photo changes nothing
    body = upload_auto(client, aid, photo)
    scan = body["scans"][0]
    assert scan["status"] == "recorded" and scan["student"] == "Sofia Rossi"
    assert body["progress"]["done"] == 1 and body["progress"]["total"] == 8 and "Sofia Rossi" not in body["progress"]["missing"]
    with app.app_context():
        attempt = Attempt.query.filter_by(assessment_id=aid, source="paper").one()
        assert attempt.score == 6.0 and attempt.status == "submitted"
    again = upload_auto(client, aid, photo_of(app, aid, "demo.student3", rotation=1, seed=5))["scans"][0]
    assert again["status"] == "replaced" and again["duplicate"] is True
    with app.app_context():
        assert Attempt.query.filter_by(assessment_id=aid, source="paper").count() == 1
    page = client.get(f"/staff/assessments/{aid}/scans")
    assert b"1 of 8 students scanned" in page.data and b"Already recorded" in page.data


def test_unsure_answers_come_with_a_guess_and_the_quick_fix_records_them(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid = choice_only_assessment(app, "Quiz with a light mark")

    def lighten(layout, marks):
        first = layout["slots"][2]["item"]  # question 3 is answered very faintly: an ink-free grey smudge
        marks[first] = []

    client.get(f"/staff/assessments/{aid}/bubble-sheets")
    photo = photo_of(app, aid, "demo.student4", seed=6, mutate=lighten)
    body = upload_auto(client, aid, photo)
    scan = body["scans"][0]
    # a blank row is not an error: it is reported, and the sheet is still recorded
    assert scan["status"] == "recorded" and scan["blank"] == [3]
    # a sheet with a real doubt goes to the quick fix instead
    with app.app_context():
        student = User.query.filter_by(username="demo.student5").one()
        a = db.session.get(Assessment, aid)
        sheet = AnswerSheet.query.filter_by(assessment_id=aid, user_id=student.id).one()
        layout = sheet.get_layout()
        marks = correct_marks(layout, a)
        slot = next(s for s in layout["slots"] if s["n"] == 2)
        marks[slot["item"]] = [0, 1]  # two bubbles filled
        photo = fake_photo(render_sheet(layout, sheet.id, marks), seed=7)
    scan = upload_auto(client, aid, photo)["scans"][0]
    assert scan["status"] == "review" and scan["flags"] == 1 and scan["fix_url"].endswith("/fix")
    panel = client.get(scan["fix_url"])
    assert panel.status_code == 200 and b"fix-row flagged" in panel.data and b"fix-bubble" in panel.data
    assert b"More than one bubble filled" in panel.data and b"Record these answers" in panel.data
    with app.app_context():
        sid = User.query.filter_by(username="demo.student5").one().id
        items = {i.id: i for i in db.session.get(Assessment, aid).items}
        form = {"user_id": sid}
        result = db.session.get(SheetScan, scan["id"]).get_result()["items"]
        for item_id, read in result.items():
            form[f"q_{item_id}"] = read["value"]
        form[f"q_{slot['item']}"] = "A"
    r = client.post(scan["fix_url"], data=form, headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json["ok"] and r.json["scan"]["status"] == "recorded"
    assert r.json["progress"]["done"] == 2
    with app.app_context():
        assert db.session.get(SheetScan, scan["id"]).status == "recorded"
    # no student chosen: refused, nothing recorded
    bad = client.post(scan["fix_url"], data={}, headers={"Accept": "application/json"})
    assert bad.status_code == 400 and bad.json["ok"] is False


def test_the_scan_page_has_the_camera(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    aid, _ = demo(app)
    r = client.get(f"/staff/assessments/{aid}/scans")
    assert b'id="open-scanner"' in r.data and b"js/scanner.js" in r.data and b"data-auto-record" in r.data
    assert b"0 of 8 students scanned" in r.data


def test_the_reader_says_what_is_wrong_with_a_photo(app):
    aid, _ = demo(app)
    with app.app_context():
        a = db.session.get(Assessment, aid)
        layout = omr.plan_pages(a)[0]
    sheet = render_sheet(layout, 5, {})
    # one corner square covered
    hidden = sheet.copy()
    off = omr.FID / 2 + 6
    P = lambda v: int(round((v + off) * 6))  # noqa: E731
    cv2.rectangle(hidden, (P(omr.FRAME_W - omr.FID), P(omr.FRAME_H - omr.FID)), (P(omr.FRAME_W + omr.FID), P(omr.FRAME_H + omr.FID)), 255, -1)
    with pytest.raises(omr.OMRError, match="Only 3 of the 4 black corner squares"):
        omr.read_photo(fake_photo(hidden, seed=2))
    # the sheet far away in a small picture
    with pytest.raises(omr.OMRError, match="too small"):
        omr.read_photo(fake_photo(sheet, seed=2, size=(560, 780)))
    with pytest.raises(omr.OMRError, match="not an image"):
        omr.read_photo(b"not a picture")


def test_the_reader_copes_with_bad_light_light_pencil_and_curled_paper():
    """Degraded phone photos from the benchmark: nothing silently wrong, and few answers need a human."""
    from omr_bench import evaluate

    stats, _, failures = evaluate(n=8, seed=21, profile="hard")
    assert stats["unreadable"] <= 1, failures
    assert stats["silent_errors"] <= 1
    assert stats["flagged"] / stats["answers"] < 0.07
    assert stats["clean_sheets"] >= 2
