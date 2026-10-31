"""Tests made from a PDF: upload the test, type in the answers, students answer beside the pages."""
import io
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_app import app, login  # noqa: F401  (fixture re-export; puts sources/ on sys.path)
from test_portal import UA, begin, sign_in
from pdf_synth import make_pdf

from scholar import pdftest, upgrade_schema
from scholar.extensions import db
from scholar.models import Assessment, Attempt, Question, User

pytestmark = pytest.mark.skipif(not pdftest.available(), reason="needs pypdfium2 and OpenCV")


def staff(app_):
    client = app_.test_client()
    login(client, "demo.instructor", "instructor123")
    return client


def course_id(app_):
    with app_.app_context():
        return Assessment.query.filter_by(title="Unit 1 Check-in").one().course_id


def create(client, app_, pdf=None, title="Chapter 4 test", questions=6, name="chapter4.pdf"):
    r = client.post(
        "/staff/assessments/new-from-pdf",
        data={"course_id": course_id(app_), "title": title, "questions": str(questions), "choices": "4", "pdf": (io.BytesIO(pdf or make_pdf(3)), name)},
        content_type="multipart/form-data",
    )
    assert r.status_code == 302, r.data[:600]
    return int(re.search(r"/assessments/(\d+)/key", r.headers["Location"]).group(1))


def key_form(*answers, types=None, points=None):
    """answers: 'B', 'AC', '12.5', 'zero | 0' … in question order."""
    form = {"rows": str(len(answers))}
    for n, answer in enumerate(answers, start=1):
        kind = (types or {}).get(n, "mc")
        form[f"type_{n}"] = kind
        form[f"choices_{n}"] = "2" if kind == "tf" else "4"
        form[f"answer_{n}"] = answer
        form[f"points_{n}"] = str((points or {}).get(n, 1))
        form[f"tol_{n}"] = "0.5" if kind == "numeric" else "0"
    return form


def save(client, aid, form, follow=True):
    return client.post(f"/staff/assessments/{aid}/key", data=form, follow_redirects=follow)


def publish(client, aid):
    client.post(f"/staff/assessments/{aid}/status", data={"action": "publish"})


def code_of(app_, aid):
    with app_.app_context():
        return db.session.get(Assessment, aid).access_code


def correct_form(app_, attempt_id):
    with app_.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        form = {}
        for item in attempt.assessment.items:
            q = item.question
            form[f"present_{item.id}"] = "1"
            if q.qtype in ("mc", "tf"):
                form[f"q_{item.id}"] = str(q.correct_choices[0].id)
            elif q.qtype == "ms":
                form[f"q_{item.id}"] = [str(c.id) for c in q.correct_choices]
            elif q.qtype == "numeric":
                form[f"q_{item.id}"] = str(q.numeric_answer + 0.25)
            elif q.qtype == "short":
                form[f"q_{item.id}"] = q.choices[0].text.upper()
        return form


# --------------------------------------------------------------------------- creating and entering the key


def test_upload_a_pdf_and_enter_the_answers(app):
    client = staff(app)
    aid = create(client, app, questions=8)
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert a.status == "draft" and a.pdf.pages == 3 and len(a.pdf_pages) == 3 and not a.items
        assert a.pdf.filename == "chapter4.pdf" and a.pdf.sha256 and len(a.access_code) == 8
    page = client.get(f"/staff/assessments/{aid}/key?rows=8&choices=4")
    assert page.status_code == 200 and page.data.count(b'class="key-row"') == 8 + 1 and page.data.count(b"/pdf/page/") == 3
    assert b"Paste the answers" in page.data and b"js/pdf-key.js" in page.data
    # mixed types: choice, true/false, several, number with an allowed error, words, hand-graded
    form = key_form("B", "T", "AC", "12.5", "zero | 0", "", "D", types={2: "tf", 3: "ms", 4: "numeric", 5: "short", 6: "essay"}, points={6: 4, 7: 2})
    r = save(client, aid, form)
    assert b"Answer key saved: 7 questions, 11 points" in r.data
    with app.app_context():
        items = sorted(db.session.get(Assessment, aid).items, key=lambda i: i.position)
        qs = [i.question for i in items]
        assert [q.qtype for q in qs] == ["mc", "tf", "ms", "numeric", "short", "essay", "mc"]
        assert [q.stem for q in qs][:2] == ["Question 1", "Question 2"]
        assert [c.text for c in qs[0].choices] == ["A", "B", "C", "D"] and [c.is_correct for c in qs[0].choices] == [False, True, False, False]
        assert [c.text for c in qs[1].choices] == ["True", "False"] and qs[1].choices[0].is_correct
        assert [c.is_correct for c in qs[2].choices] == [True, False, True, False]
        assert qs[3].numeric_answer == 12.5 and qs[3].numeric_tolerance == 0.5
        assert sorted(c.text for c in qs[4].choices) == ["0", "zero"]
        assert qs[5].points == 4 and qs[6].points == 2
        assert all(q.archived and not q.shared and q.topic == "PDF: chapter4.pdf" for q in qs)
    # the questions stay out of the question bank, and the key reloads as entered
    assert b"Question 3" not in client.get("/staff/questions/").data
    again = client.get(f"/staff/assessments/{aid}/key").data.decode()
    assert 'name="answer_3" value="AC"' in again and 'name="answer_4" value="12.5"' in again and "zero | 0" in again


def test_a_key_with_mistakes_is_refused_with_reasons(app):
    client = staff(app)
    aid = create(client, app)
    bad = key_form("B", "", "E", "x", "", "A", types={4: "numeric", 5: "short"})
    r = save(client, aid, bad)
    for text in (b"Question 2: choose the correct answer", b"Question 3: choose the correct answer", b"Question 4: type the correct number", b"Question 5: type the correct answer"):
        assert text in r.data, text
    with app.app_context():
        assert not db.session.get(Assessment, aid).items  # nothing half-saved
    assert b"at least one question" in save(client, aid, {"rows": "0"}).data
    # spare empty rows at the end are simply ignored
    assert b"saved: 2 questions" in save(client, aid, key_form("A", "C", "", "", "")).data


def test_quick_key_parser():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).resolve().parent.parent / "sources" / "scholar" / "static" / "js" / "pdf-key.js"
    cases = {
        "1.B 2.D 3.A": [[1, "B"], [2, "D"], [3, "A"]],
        "1 B\n2 D\n3 AC": [[1, "B"], [2, "D"], [3, "AC"]],
        "BDACC": [[1, "B"], [2, "D"], [3, "A"], [4, "C"], [5, "C"]],
        "B D A C": [[1, "B"], [2, "D"], [3, "A"], [4, "C"]],
        "AC BD A": [[1, "AC"], [2, "BD"], [3, "A"]],
        "1) T 2) F": [[1, "T"], [2, "F"]],
        "1: 12.5 2: 7": [[1, "12.5"], [2, "7"]],
        "1-B,2-D": [[1, "B"], [2, "D"]],
        "": [],
    }
    for text, want in cases.items():
        out = subprocess.run([node, "-e", f"console.log(JSON.stringify(require({str(script)!r}).parseKey({text!r})))"], capture_output=True, text=True, check=True)
        assert out.stdout.strip().replace(" ", "") == str(want).replace("'", '"').replace(" ", ""), (text, out.stdout)


def test_bad_files_are_explained(app):
    client = staff(app)
    cid = course_id(app)

    def post(data, name="t.pdf"):
        return client.post("/staff/assessments/new-from-pdf", data={"course_id": cid, "pdf": (io.BytesIO(data), name)}, content_type="multipart/form-data", follow_redirects=True)

    assert b"not a PDF" in post(b"just some text").data
    assert b"could not open this PDF" in post(make_pdf(2)[:400]).data
    assert b"at most 60" in post(make_pdf(61, 1)).data
    assert b"Choose the test as a PDF file" in client.post("/staff/assessments/new-from-pdf", data={"course_id": cid}, follow_redirects=True).data
    with app.app_context():
        assert not Assessment.query.filter(Assessment.pdf != None).count()  # noqa: E711


# --------------------------------------------------------------------------- the staff view


def test_staff_pages_for_a_pdf_test(app):
    client = staff(app)
    aid = create(client, app)
    save(client, aid, key_form("B", "D", "A"))
    pdf = client.get(f"/staff/assessments/{aid}/pdf")
    assert pdf.mimetype == "application/pdf" and pdf.data.startswith(b"%PDF-")
    img = client.get(f"/staff/assessments/{aid}/pdf/page/2.jpg")
    assert img.mimetype == "image/jpeg" and img.data[:3] == b"\xff\xd8\xff"
    assert client.get(f"/staff/assessments/{aid}/pdf/page/9.jpg").status_code == 404
    detail = client.get(f"/staff/assessments/{aid}")
    assert b"Edit the answer key" in detail.data and b"Write a new question" not in detail.data and b"Answer key</a>" in detail.data
    assert client.get(f"/staff/assessments/{aid}/preview").data.count(b"/pdf/page/") == 3
    assert client.get(f"/staff/assessments/{aid}/paper").status_code == 200
    assert b"The test (PDF)" in client.get(f"/staff/assessments/{aid}/paper").data
    assert client.get(f"/staff/assessments/{aid}/bubble-sheets").data.count(b'class="omr-sheet"') == 8
    # questions of a PDF test aren't edited like bank questions, nor added to
    with app.app_context():
        qid = Question.query.filter_by(topic="PDF: chapter4.pdf").first().id
        item_id = db.session.get(Assessment, aid).items[0].id
    assert f"/staff/assessments/{aid}/key" in client.get(f"/staff/questions/{qid}/edit").headers["Location"]
    assert client.post(f"/staff/assessments/{aid}/items/add", data={"question_ids": "1"}).headers["Location"].endswith("/key")
    assert client.post(f"/staff/assessments/{aid}/items/{item_id}/remove").headers["Location"].endswith("/key")
    # a PDF test keeps its order and letters: shuffling is switched off whatever the form says
    form = {"title": "Chapter 4 test", "max_attempts": "1", "show_results": "immediately", "allow_online": "1", "identity_check": "confirm",
            "shuffle_questions": "1", "shuffle_choices": "1", "sequential": "1", "lock_to_device": "1"}
    client.post(f"/staff/assessments/{aid}/settings", data=form)
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert not (a.shuffle_questions or a.shuffle_choices or a.sequential)


def test_students_answer_beside_the_pages(app):
    client = staff(app)
    aid = create(client, app)
    save(client, aid, key_form("B", "T", "AC", "12.5", "zero | 0", types={2: "tf", 3: "ms", 4: "numeric", 5: "short"}, points={3: 2}))
    publish(client, aid)
    code = code_of(app, aid)
    student = app.test_client()
    # not signed in: no pages
    assert student.get("/attempt/page/1.jpg").status_code in (302, 401)
    sign_in(student, code, "MA2026008")
    assert student.get("/attempt/page/1.jpg", headers=UA).status_code in (302, 404)  # no attempt started yet
    attempt_id = begin(app, student)
    page = student.get("/attempt", headers=UA)
    assert page.data.count(b"/attempt/page/") == 3 and b'class="pdf-body"' in page.data and b"Your answers" in page.data
    assert b'class="stem"' not in page.data
    img = student.get("/attempt/page/2.jpg", headers=UA)
    assert img.mimetype == "image/jpeg" and img.headers["Cache-Control"].startswith("no-store") and img.data[:2] == b"\xff\xd8"
    assert student.get("/attempt/page/7.jpg", headers=UA).status_code == 404
    form = correct_form(app, attempt_id)
    student.post("/attempt/save", data=form, headers=UA)
    r = student.post("/attempt/submit", data=form, headers=UA, follow_redirects=True)
    assert b"6 of 6 points" in r.data
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.score == 6.0 and attempt.max_score == 6.0 and attempt.status == "submitted"
    # once submitted, the pages are gone
    assert student.get("/attempt/page/1.jpg", headers=UA).status_code == 404
    # another student can't borrow the first one's session for the pages of a different test
    other = app.test_client()
    assert other.get("/attempt/page/1.jpg").status_code in (302, 401)


def test_a_paused_student_cannot_read_the_test(app):
    client = staff(app)
    aid = create(client, app)
    save(client, aid, key_form("A", "B"))
    publish(client, aid)
    student = app.test_client()
    sign_in(student, code_of(app, aid), "MA2026008")
    attempt_id = begin(app, student)
    assert student.get("/attempt/page/1.jpg", headers=UA).status_code == 200
    with app.app_context():
        uid = db.session.get(Attempt, attempt_id).user_id
    client.post(f"/staff/assessments/{aid}/monitor/student/{uid}/lock")
    assert student.get("/attempt/page/1.jpg", headers=UA).status_code == 404


# --------------------------------------------------------------------------- after students have started


def test_correcting_the_key_rescores_but_the_structure_is_locked(app):
    client = staff(app)
    aid = create(client, app)
    save(client, aid, key_form("A", "B", "C"))
    publish(client, aid)
    student = app.test_client()
    sign_in(student, code_of(app, aid), "MA2026008")
    attempt_id = begin(app, student)
    form = correct_form(app, attempt_id)
    student.post("/attempt/submit", data=form, headers=UA)
    with app.app_context():
        assert db.session.get(Attempt, attempt_id).score == 3.0
    page = client.get(f"/staff/assessments/{aid}/key").data
    assert b"Students have already started this test" in page and b'name="type_1" value="mc"' in page
    # the publisher's key was wrong on question 2: B is not right, D is. Everyone is re-scored.
    r = save(client, aid, key_form("A", "D", "C"))
    assert b"1 attempt(s) re-scored" in r.data
    with app.app_context():
        assert db.session.get(Attempt, attempt_id).score == 2.0
    # but questions can't be added, removed or retyped
    assert b"can't be added or removed" in save(client, aid, key_form("A", "D", "C", "B")).data.replace(b"&#39;", b"'")
    assert b"can't change" in save(client, aid, key_form("A", "T", "C", types={2: "tf"})).data.replace(b"&#39;", b"'")
    with app.app_context():
        assert len(db.session.get(Assessment, aid).items) == 3


# --------------------------------------------------------------------------- copies, deletion, replacing


def test_duplicates_have_their_own_key_and_deleting_cleans_up(app):
    client = staff(app)
    aid = create(client, app)
    save(client, aid, key_form("A", "B", "C"))
    r = client.post(f"/staff/assessments/{aid}/duplicate", data={})
    copy = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        original, clone = db.session.get(Assessment, aid), db.session.get(Assessment, copy)
        assert clone.pdf.sha256 == original.pdf.sha256 and len(clone.pdf_pages) == 3
        assert {i.question_id for i in clone.items}.isdisjoint({i.question_id for i in original.items})
    save(client, copy, key_form("D", "D", "D"))
    with app.app_context():
        assert [i.question.correct_choices[0].text for i in sorted(db.session.get(Assessment, aid).items, key=lambda i: i.position)] == ["A", "B", "C"]
    # deleting an assessment takes its PDF questions with it
    before = None
    with app.app_context():
        before = Question.query.filter_by(topic="PDF: chapter4.pdf").count()
        assert before == 6
    client.post(f"/staff/assessments/{copy}/delete", data={})
    with app.app_context():
        assert Question.query.filter_by(topic="PDF: chapter4.pdf").count() == 3
        assert db.session.get(Assessment, copy) is None
    # replacing the PDF keeps the key
    r = client.post(f"/staff/assessments/{aid}/pdf/replace", data={"pdf": (io.BytesIO(make_pdf(5)), "second.pdf")}, content_type="multipart/form-data", follow_redirects=True)
    assert b"The PDF was replaced (5 pages)" in r.data
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert a.pdf.pages == 5 and len(a.pdf_pages) == 5 and len(a.items) == 3
    assert b"not a PDF" in client.post(f"/staff/assessments/{aid}/pdf/replace", data={"pdf": (io.BytesIO(b"nope"), "x.pdf")}, content_type="multipart/form-data", follow_redirects=True).data


def test_only_the_course_instructors_reach_the_pdf(app):
    client = staff(app)
    aid = create(client, app)
    save(client, aid, key_form("A"))
    with app.app_context():
        from scholar.models import Course

        other = User(username="other.teacher", full_name="Other", role="instructor")
        other.set_password("otherpass123")
        db.session.add(other)
        db.session.commit()
    stranger = app.test_client()
    login(stranger, "other.teacher", "otherpass123")
    for path in (f"/staff/assessments/{aid}/pdf", f"/staff/assessments/{aid}/pdf/page/1.jpg", f"/staff/assessments/{aid}/key"):
        assert stranger.get(path).status_code == 403, path
    assert stranger.post(f"/staff/assessments/{aid}/key", data=key_form("B")).status_code == 403
    assert stranger.get(f"/staff/assessments/new-from-pdf?course_id={course_id(app)}").status_code == 403
    with app.app_context():
        assert [i.question.correct_choices[0].text for i in db.session.get(Assessment, aid).items] == ["A"]


def test_the_database_upgrade_adds_the_pdf_tables(app):
    from sqlalchemy import inspect

    with app.app_context():
        db.session.execute(db.text("DROP TABLE assessment_pdf_pages"))
        db.session.execute(db.text("DROP TABLE assessment_pdfs"))
        db.session.commit()
        upgrade_schema()
        names = set(inspect(db.engine).get_table_names())
        assert {"assessment_pdfs", "assessment_pdf_pages"} <= names
