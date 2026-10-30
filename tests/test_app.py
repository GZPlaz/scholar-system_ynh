"""End-to-end tests for Scholar System (run with ``pytest`` from the repository root).

Uses a temporary SQLite database by default. Set SCHOLAR_TEST_DATABASE_URL to run the same
suite against PostgreSQL (the database is wiped), e.g.
``postgresql+psycopg2://scholar:pw@127.0.0.1:5432/scholar_test``.
"""
import io
import os
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "sources"))
os.environ["SCHOLAR_CONFIG"] = "/nonexistent"

from scholar import create_app, upgrade_schema  # noqa: E402
from scholar.analytics import course_mastery, item_analysis  # noqa: E402
from scholar.demo import seed  # noqa: E402
from scholar.extensions import db  # noqa: E402
from scholar.grading import counted_attempts  # noqa: E402
from scholar.models import Assessment, Attempt, Course, Question, Response, User, utcnow  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SQLALCHEMY_DATABASE_URI": os.environ.get("SCHOLAR_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}",
            "SSO_ENABLED": True,
            "ADMIN_USERNAME": "boss",
        }
    )
    with app.app_context():
        db.drop_all()
        upgrade_schema()
        admin = User(username="admin", full_name="Admin", role="admin")
        admin.set_password("adminpass123")
        db.session.add(admin)
        db.session.commit()
        seed()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


def login(client, username, password):
    return client.post("/staff/login", data={"username": username, "password": password}, follow_redirects=True)


def test_login_logout_and_failures(app, caplog):
    client = app.test_client()
    assert client.get("/staff/").status_code == 302
    r = login(client, "admin", "wrong")
    assert b"Invalid username or password" in r.data
    assert "Failed login attempt for admin from" in caplog.text  # fail2ban regex anchor
    r = login(client, "admin", "adminpass123")
    assert b"Welcome, Admin" in r.data
    r = client.post("/staff/logout", follow_redirects=True)
    assert b"signed out" in r.data


def test_sso_header_login_and_admin_bootstrap(app):
    client = app.test_client()
    r = client.get("/staff/", headers={"X-Scholar-User": "boss"}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        boss = User.query.filter_by(username="boss").one()
        assert boss.role == "admin" and boss.auth_source == "ldap"
    # after logout the header no longer signs the user back in
    client.post("/staff/logout")
    r = client.get("/staff/", headers={"X-Scholar-User": "boss"})
    assert r.status_code == 302 and "/staff/login" in r.headers["Location"]


def test_sso_disabled_ignores_header(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'x.db'}", "SSO_ENABLED": False})
    with app.app_context():
        upgrade_schema()
    r = app.test_client().get("/staff/", headers={"X-Scholar-User": "mallory"})
    assert r.status_code == 302


STAFF_PAGES = [
    "/staff/", "/staff/account", "/staff/courses/", "/staff/courses/new", "/staff/questions/", "/staff/questions/new", "/staff/questions/new?qtype=essay",
    "/staff/questions/import", "/staff/standards/", "/staff/standards/new", "/staff/standards/import",
    "/staff/admin/users", "/staff/admin/users/new", "/staff/admin/users/import", "/staff/admin/settings", "/staff/admin/audit",
]


def test_every_staff_page_renders(app):
    client = app.test_client()
    login(client, "admin", "adminpass123")
    with app.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        assessment = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        attempt = assessment.attempts[0]
        question = Question.query.first()
        item = assessment.items[-1]
        student = course.students[0]
        pages = STAFF_PAGES + [
            f"/staff/courses/{course.id}", f"/staff/courses/{course.id}/edit", f"/staff/courses/{course.id}/roster",
            f"/staff/reports/course/{course.id}/gradebook", f"/staff/reports/course/{course.id}/gradebook?format=csv",
            f"/staff/reports/course/{course.id}/mastery", f"/staff/reports/course/{course.id}/mastery?format=csv",
            f"/staff/reports/course/{course.id}/student/{student.id}",
            f"/staff/assessments/new?course_id={course.id}",
            f"/staff/assessments/{assessment.id}", f"/staff/assessments/{assessment.id}?bank=1", f"/staff/assessments/{assessment.id}/settings",
            f"/staff/assessments/{assessment.id}/results", f"/staff/assessments/{assessment.id}/results?format=csv",
            f"/staff/assessments/{assessment.id}/analysis", f"/staff/assessments/{assessment.id}/analysis?format=csv",
            f"/staff/assessments/{assessment.id}/grading", f"/staff/assessments/{assessment.id}/grading/item/{item.id}",
            f"/staff/assessments/{assessment.id}/attempts/{attempt.id}", f"/staff/assessments/{assessment.id}/preview",
            f"/staff/assessments/{assessment.id}/print", f"/staff/assessments/{assessment.id}/print?key=1",
            f"/staff/assessments/{assessment.id}/bubble-sheets", f"/staff/assessments/{assessment.id}/bubble-sheets?blank=1",
            f"/staff/assessments/{assessment.id}/paper", f"/staff/assessments/{assessment.id}/paper/{student.id}",
            f"/staff/questions/{question.id}", f"/staff/questions/{question.id}/edit",
            f"/staff/admin/users/{student.id}/edit",
            f"/staff/assessments/{assessment.id}/monitor", f"/staff/assessments/{assessment.id}/monitor?fragment=1",
            f"/staff/assessments/{assessment.id}/insights", f"/staff/assessments/{assessment.id}/insights?format=csv",
            "/staff/admin/integrations",
        ]
    for page in pages:
        r = client.get(page)
        assert r.status_code == 200, (page, r.status_code, r.data[:500])


def test_manual_grading_and_regrade(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    with app.app_context():
        assessment = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        essay_item = next(i for i in assessment.items if i.question.qtype == "essay")
        pending = Response.query.filter_by(item_id=essay_item.id, needs_grading=True).all()
        assert pending
        data = {f"points_{r.id}": "3" for r in pending}
        data.update({f"feedback_{r.id}": "Good" for r in pending})
        aid, item_id = assessment.id, essay_item.id
    r = client.post(f"/staff/assessments/{aid}/grading/item/{item_id}", data=data, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        assessment = db.session.get(Assessment, aid)
        assert not any(a.needs_grading for a in assessment.attempts)
        before = {a.id: a.score for a in assessment.attempts}
        # change the key of the first MC question → every attempt is re-scored
        q = next(i.question for i in assessment.items if i.question.qtype == "mc")
        old_key = q.correct_choices[0]
        new_key = next(c for c in q.choices if not c.is_correct)
        form = {"qtype": "mc", "stem": q.stem, "points": "1", "shared": "1",
                "choice_text": [c.text for c in q.choices], "choice_id": [str(c.id) for c in q.choices],
                "correct": [str(q.choices.index(new_key))], "standards": [str(s.id) for s in q.standards]}
        qid = q.id
        old_key_id = old_key.id
    r = client.post(f"/staff/questions/{qid}/edit", data=form, follow_redirects=True)
    assert b"re-scored" in r.data
    with app.app_context():
        assessment = db.session.get(Assessment, aid)
        after = {a.id: a.score for a in assessment.attempts}
        assert before != after
        # manual essay grades survive the regrade
        assert all(r.points_awarded == 3 for r in Response.query.filter_by(item_id=item_id).all())
        assert db.session.get(Question, qid).choices[0].id in (old_key_id, *[c.id for c in db.session.get(Question, qid).choices])


def test_question_types_validation(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    r = client.post("/staff/questions/new", data={"qtype": "mc", "stem": "Pick", "points": "1", "choice_text": ["a", "b"], "correct": ["0", "1"]})
    assert b"exactly one option" in r.data
    r = client.post("/staff/questions/new", data={"qtype": "numeric", "stem": "pi?", "points": "2", "numeric_answer": "3.14", "numeric_tolerance": "0.01"}, follow_redirects=True)
    assert b"Question saved" in r.data
    r = client.post("/staff/questions/new", data={"qtype": "short", "stem": "Capital of France", "points": "1", "accepted_answers": "Paris\nparis"}, follow_redirects=True)
    assert b"Question saved" in r.data


def test_csv_imports(app):
    client = app.test_client()
    login(client, "admin", "adminpass123")
    csv_std = "code,title,subject\nBIO.1,Cells,Biology\nBIO.2,Genetics,Biology\n"
    r = client.post("/staff/standards/import", data={"csv_text": csv_std}, follow_redirects=True)
    assert b"2 standard(s) created" in r.data
    csv_q = (
        "type,question,choices,correct,points,standards\n"
        "mc,Powerhouse of the cell?,Nucleus|Mitochondria|Ribosome,B,1,BIO.1\n"
        "ms,Which are nucleotides?,Adenine|Glucose|Thymine,A|C,2,BIO.2\n"
        "tf,DNA is double stranded.,,true,1,BIO.2\n"
        "numeric,How many chromosomes in a human cell?,,46,1,BIO.2\n"
        "bogus,Nope,,,1,\n"
    )
    r = client.post("/staff/questions/import", data={"csv_text": csv_q}, follow_redirects=True)
    assert b"4 question(s) imported" in r.data and b"unknown type" in r.data
    csv_users = "username,full_name,role,student_id,course\nnew.kid,New Kid,student,MA9,DEMO101\nnew.prof,New Prof,instructor,,DEMO101\n"
    r = client.post("/staff/admin/users/import", data={"csv_text": csv_users})
    # only the instructor gets a generated password; the student is a test taker without one
    assert r.mimetype == "text/csv" and b"new.prof" in r.data and b"new.kid" not in r.data
    with app.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        assert any(s.username == "new.kid" for s in course.students)
        assert any(i.username == "new.prof" for i in course.instructors)
        kid = User.query.filter_by(username="new.kid").one()
        assert kid.password_hash is None and kid.auth_source == "portal" and kid.student_number == "MA9"
        assert User.query.filter_by(username="new.prof").one().must_change_password


def test_paper_entry_and_import(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    with app.app_context():
        assessment = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        student = User.query.filter_by(username="demo.student8").one()
        items = list(assessment.items)
        values = {}
        letters = []
        for item in items:
            q = item.question
            if q.qtype in ("mc", "tf", "ms"):
                value = "".join(chr(65 + i) for i, c in enumerate(q.choices) if c.is_correct)
            elif q.qtype == "short":
                value = "zero"
            elif q.qtype == "numeric":
                value = "5"
            else:
                value = "4"
            values[f"q_{item.id}"] = value
            letters.append(value)
        aid, sid = assessment.id, student.id
    r = client.post(f"/staff/assessments/{aid}/paper/{sid}", data=values, follow_redirects=True)
    assert b"Responses saved" in r.data
    with app.app_context():
        attempt = Attempt.query.filter_by(assessment_id=aid, user_id=sid, source="paper").one()
        assert attempt.score == attempt.max_score == 13.0
    # bad letter is rejected and nothing changes
    bad = dict(values)
    bad[f"q_{items[0].id}"] = "Z"
    r = client.post(f"/staff/assessments/{aid}/paper/{sid}", data=bad, follow_redirects=True)
    assert b"Not saved" in r.data
    # CSV import for another student (all blank → zero, essay pending)
    header = "username," + ",".join(f"q{i + 1}" for i in range(len(letters)))
    csv_text = header + "\n" + "demo.student7," + ",".join(letters[:-1]) + ",\nghost," + ",".join(letters) + "\n"
    r = client.post(f"/staff/assessments/{aid}/paper/import", data={"file": (io.BytesIO(csv_text.encode()), "paper.csv")}, content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported responses for 1 student" in r.data and b"ghost" in r.data
    with app.app_context():
        user = User.query.filter_by(username="demo.student7").one()
        attempt = Attempt.query.filter_by(assessment_id=aid, user_id=user.id, source="paper").one()
        assert attempt.score == 9.0 and attempt.needs_grading


def test_builder_workflow(app):
    client = app.test_client()
    login(client, "demo.instructor", "instructor123")
    with app.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        cid = course.id
        qids = [q.id for q in Question.query.limit(3)]
    r = client.post("/staff/assessments/new", data={"course_id": cid, "title": "Quiz 2", "max_attempts": "1", "show_results": "after_close", "allow_online": "1", "opens_at": "2026-01-01T09:00", "closes_at": "2025-01-01T09:00"}, follow_redirects=True)
    assert b"closing time must be after" in r.data
    r = client.post("/staff/assessments/new", data={"course_id": cid, "title": "Quiz 2", "max_attempts": "1", "show_results": "after_close", "allow_online": "1"})
    aid = int(re.search(r"/staff/assessments/(\d+)", r.headers["Location"]).group(1))
    r = client.post(f"/staff/assessments/{aid}/status", data={"action": "publish"}, follow_redirects=True)
    assert b"at least one question" in r.data
    client.post(f"/staff/assessments/{aid}/items/add", data={"question_ids": [str(q) for q in qids]})
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert [i.question_id for i in a.items] == qids
        second = a.items[1].id
    client.post(f"/staff/assessments/{aid}/items/{second}/up")
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert [i.question_id for i in sorted(a.items, key=lambda i: i.position)] == [qids[1], qids[0], qids[2]]
    client.post(f"/staff/assessments/{aid}/status", data={"action": "publish"})
    r = client.post(f"/staff/assessments/{aid}/duplicate", data={"course_id": cid}, follow_redirects=True)
    assert b"Quiz 2 (copy)" in r.data


def test_analytics(app):
    with app.app_context():
        assessment = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        data = item_analysis(assessment)
        assert data["n"] == 7
        assert len(data["items"]) == len(assessment.items)
        assert all(e["p_value"] is None or 0 <= e["p_value"] <= 1 for e in data["items"])
        essay = next(e for e in data["items"] if e["question"].qtype == "essay")
        assert essay["pending"] == 7 and essay["p_value"] is None
        assert data["summary"]["bins"] and sum(data["summary"]["bins"]) == 7
        standards, per_user = course_mastery(assessment.course)
        assert {s.code for s in standards} == {"MATH.ALG.1", "MATH.ALG.2", "MATH.CALC.1", "MATH.CALC.2"}
        assert len(per_user) == 7
        assert len(counted_attempts(assessment)) == 7


def test_prefix_middleware(tmp_path):
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'p.db'}", "APP_PREFIX": "/scholar"})
    with app.app_context():
        upgrade_schema()
    client = app.test_client()
    r = client.get("/scholar/staff/login")
    assert r.status_code == 200 and b'action="/scholar/staff/login"' in r.data
    assert b"Take a test" in client.get("/scholar/").data
    # old addresses keep their prefix
    assert client.get("/scholar/login").headers["Location"].replace("http://localhost", "") == "/scholar/staff/login"


def test_ldap_login_provisions_yunohost_user(app, monkeypatch):
    """Exercise the YunoHost LDAP path against ldap3's in-memory mock directory."""
    import ldap3

    server = ldap3.Server("mock_yunohost")
    seed_conn = ldap3.Connection(server, client_strategy=ldap3.MOCK_SYNC)
    seed_conn.strategy.add_entry(
        "uid=alice,ou=users,dc=yunohost,dc=org",
        {"uid": "alice", "userPassword": "s3cret-pass", "cn": "Alice Ynh", "mail": "alice@mindaccel.org", "objectClass": "inetOrgPerson"},
    )
    real_connection = ldap3.Connection
    monkeypatch.setattr(ldap3, "Server", lambda *a, **k: server)
    monkeypatch.setattr(ldap3, "Connection", lambda srv, **k: real_connection(srv, client_strategy=ldap3.MOCK_SYNC, **{x: y for x, y in k.items() if x in ("user", "password")}))
    app.config["LDAP_ENABLED"] = True
    client = app.test_client()
    r = login(client, "alice", "wrong")
    assert b"Invalid username or password" in r.data
    # unknown YunoHost users get no account unless the administrator allows it...
    r = login(client, "alice", "s3cret-pass")
    assert r.status_code == 403 and b"not registered in Scholar" in r.data
    # ...and then only as instructors: students never have staff-site accounts
    with app.app_context():
        from scholar.utils import set_setting

        set_setting("ldap_autoprovision", "1")
        db.session.commit()
    r = login(client, "alice", "s3cret-pass")
    assert b"Welcome, Alice Ynh" in r.data
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        assert alice.auth_source == "ldap" and alice.role == "instructor" and alice.email == "alice@mindaccel.org"
        assert alice.password_hash is None
    client.post("/staff/logout")
    # a student with a valid YunoHost password is sent to the test portal, not signed in
    with app.app_context():
        alice = User.query.filter_by(username="alice").one()
        alice.role = "student"
        db.session.commit()
    r = login(client, "alice", "s3cret-pass")
    assert r.status_code == 403 and b"test portal" in r.data
    assert client.get("/staff/").status_code == 302
    # a local account with the same name can't be hijacked through LDAP, and vice versa
    r = login(client, "demo.student1", "s3cret-pass")
    assert b"Invalid username or password" in r.data
