"""Integration API used by the RosarioSIS plugin."""
import re
import sqlite3

from test_app import app, login  # noqa: F401  (fixture re-export; puts sources/ on sys.path)

from scholar import SCHEMA_VERSION, create_app, upgrade_schema
from scholar.extensions import db
from scholar.models import ApiToken, Assessment, Course, User
from scholar.views.api import hash_token, new_token


def make_token(app_, name="RosarioSIS"):
    with app_.app_context():
        secret, prefix = new_token()
        db.session.add(ApiToken(name=name, prefix=prefix, token_hash=hash_token(secret)))
        db.session.commit()
        return secret


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_tokens_are_required_and_revocable(app):
    c = app.test_client()
    assert c.get("/api/v1/ping").status_code == 401
    assert c.get("/api/v1/ping", headers=auth("sch_wrong")).status_code == 401
    token = make_token(app)
    r = c.get("/api/v1/ping", headers=auth(token))
    assert r.status_code == 200 and r.json["app"] == "scholar"
    with app.app_context():
        t = ApiToken.query.one()
        assert t.token_hash != token and t.last_used_at is not None
        t.revoked = True
        db.session.commit()
    assert c.get("/api/v1/ping", headers=auth(token)).status_code == 401


def test_api_works_with_csrf_enabled(app):
    app.config["WTF_CSRF_ENABLED"] = True
    token = make_token(app)
    r = app.test_client().post("/api/v1/courses", json={"code": "BIO1", "title": "Biology"}, headers=auth(token))
    assert r.status_code == 201


def test_create_course_is_idempotent_by_external_id_and_patch_archives(app):
    c = app.test_client()
    token = make_token(app)
    body = {"code": "MATH-101", "title": "Algebra", "term": "2026-2027", "external_id": "rosario:cp:15"}
    r1 = c.post("/api/v1/courses", json=body, headers=auth(token))
    r2 = c.post("/api/v1/courses", json=body, headers=auth(token))
    assert r1.status_code == 201 and r2.status_code == 200 and r1.json["course"]["id"] == r2.json["course"]["id"]
    cid = r1.json["course"]["id"]
    assert c.get("/api/v1/courses?external_id=rosario:cp:15", headers=auth(token)).json["courses"][0]["id"] == cid
    assert c.post("/api/v1/courses", json={"title": "x"}, headers=auth(token)).status_code == 400
    r = c.patch(f"/api/v1/courses/{cid}", json={"archived": True, "title": "Algebra I"}, headers=auth(token))
    assert r.json["course"]["archived"] is True and r.json["course"]["title"] == "Algebra I"
    assert cid not in [x["id"] for x in c.get("/api/v1/courses", headers=auth(token)).json["courses"]]
    assert cid in [x["id"] for x in c.get("/api/v1/courses?include_archived=1", headers=auth(token)).json["courses"]]
    # an external id can only be linked to one course
    other = c.post("/api/v1/courses", json={"code": "X", "title": "Y"}, headers=auth(token)).json["course"]["id"]
    assert c.patch(f"/api/v1/courses/{other}", json={"external_id": "rosario:cp:15"}, headers=auth(token)).status_code == 409
    assert c.get("/api/v1/courses/99999", headers=auth(token)).status_code == 404


def test_roster_sync_creates_enrolls_unenrolls_and_promotes(app):
    c = app.test_client()
    token = make_token(app)
    cid = c.post("/api/v1/courses", json={"code": "HIS", "title": "History", "external_id": "rosario:cp:7"}, headers=auth(token)).json["course"]["id"]
    roster = {
        "account_type": "local",
        "instructors": [{"external_id": "rosario:staff:2", "username": "teacher", "full_name": "Ms Teacher", "email": "t@x.org"}],
        "students": [
            {"external_id": "rosario:student:1", "username": "ann", "full_name": "Ann A", "student_number": "1"},
            {"external_id": "rosario:student:2", "username": "bob", "full_name": "Bob B", "student_number": "2"},
            {"external_id": "rosario:student:3", "username": "demo.student1", "full_name": "Amara (claimed)"},
        ],
    }
    r = c.put(f"/api/v1/courses/{cid}/roster", json=roster, headers=auth(token))
    assert r.status_code == 200, r.json
    assert sorted(r.json["enrolled"]) == ["ann", "bob", "demo.student1"]
    created = {x["username"]: x for x in r.json["created"]}
    assert set(created) == {"teacher", "ann", "bob"}
    # only staff get a password; students are test takers who use the test portal
    assert created["teacher"]["temporary_password"] and created["ann"]["temporary_password"] is None
    with app.app_context():
        ann = User.query.filter_by(username="ann").one()
        assert ann.password_hash is None and ann.auth_source == "portal" and ann.student_number == "1"
    with app.app_context():
        assert User.query.filter_by(username="demo.student1").one().external_id == "rosario:student:3"
        teacher = User.query.filter_by(username="teacher").one()
        assert teacher.role == "instructor" and teacher.must_change_password
        course = db.session.get(Course, cid)
        assert course.has_instructor(teacher) and len(course.enrollments) == 3
    # bob drops, the teacher changes, a username clash is reported instead of hijacking an account
    roster["students"] = roster["students"][:1] + [{"external_id": "rosario:student:9", "username": "ann", "full_name": "Other Ann"}]
    roster["instructors"] = [{"external_id": "rosario:staff:5", "username": "newteacher", "full_name": "Mr New"}]
    roster["account_type"] = "ldap"
    r = c.put(f"/api/v1/courses/{cid}/roster", json=roster, headers=auth(token))
    assert sorted(r.json["unenrolled"]) == ["bob", "demo.student1"]
    assert r.json["conflicts"][0]["username"] == "ann"
    assert r.json["course"]["instructors"] == ["newteacher"]
    with app.app_context():
        assert User.query.filter_by(username="newteacher").one().auth_source == "ldap"
    assert c.put(f"/api/v1/courses/{cid}/roster", json={"students": [{"username": "bad name!"}]}, headers=auth(token)).status_code == 400


def test_results_report_counted_attempts(app):
    c = app.test_client()
    token = make_token(app)
    with app.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        cid = course.id
        user = User.query.filter_by(username="demo.student1").one()
        user.external_id = "rosario:student:31"
        db.session.commit()
    r = c.get(f"/api/v1/courses/{cid}/results", headers=auth(token))
    assert r.status_code == 200
    a = r.json["assessments"][0]
    assert a["title"] == "Unit 1 Check-in" and a["max_score"] == 13 and len(a["results"]) == 7
    mine = next(x for x in a["results"] if x["username"] == "demo.student1")
    assert mine["external_id"] == "rosario:student:31" and mine["needs_grading"] is True and mine["submitted_at"].endswith("Z")
    with app.app_context():
        Assessment.query.filter_by(title="Unit 1 Check-in").one().status = "draft"
        db.session.commit()
    assert c.get(f"/api/v1/courses/{cid}/results", headers=auth(token)).json["assessments"] == []


def test_deactivating_students_who_left(app):
    c = app.test_client()
    token = make_token(app)
    with app.app_context():
        for i, name in enumerate(["demo.student1", "demo.student2", "demo.student3"], start=1):
            User.query.filter_by(username=name).one().external_id = f"rosario:student:{i}"
        db.session.commit()
    r = c.post("/api/v1/users/active", json={"prefix": "rosario:student:", "active_external_ids": ["rosario:student:1"]}, headers=auth(token))
    assert r.json == {"deactivated": 2, "reactivated": 0}
    with app.app_context():
        assert not User.query.filter_by(username="demo.student2").one().active
        assert User.query.filter_by(username="demo.student4").one().active  # not from RosarioSIS: untouched
    assert c.post("/api/v1/users/active", json={"prefix": "r"}, headers=auth(token)).status_code == 400
    r = c.post("/api/v1/users/active", json={"prefix": "rosario:student:", "active_external_ids": ["rosario:student:1", "rosario:student:2"]}, headers=auth(token))
    assert r.json == {"deactivated": 0, "reactivated": 1}


def test_admin_creates_and_revokes_tokens(app):
    c = app.test_client()
    login(c, "admin", "adminpass123")
    r = c.post("/staff/admin/integrations", data={"action": "create", "name": "RosarioSIS"})
    assert b"Copy this token now" in r.data
    secret = re.search(rb"sch_[A-Za-z0-9_-]{20,}", r.data).group(0).decode()
    assert secret.startswith("sch_") and c.get("/api/v1/ping", headers=auth(secret)).status_code == 200
    with app.app_context():
        tid = ApiToken.query.one().id
    c.post("/staff/admin/integrations", data={"action": "revoke", "token_id": tid})
    assert c.get("/api/v1/ping", headers=auth(secret)).status_code == 401
    login(app.test_client(), "demo.instructor", "instructor123")
    other = app.test_client()
    login(other, "demo.instructor", "instructor123")
    assert other.get("/staff/admin/integrations").status_code == 403


def test_upgrade_adds_external_id_columns(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(120) NOT NULL UNIQUE, full_name VARCHAR(200) NOT NULL,
             email VARCHAR(200) NOT NULL, student_number VARCHAR(64) NOT NULL, role VARCHAR(20) NOT NULL,
             auth_source VARCHAR(20) NOT NULL, password_hash VARCHAR(256), must_change_password BOOLEAN NOT NULL,
             active BOOLEAN NOT NULL, created_at DATETIME NOT NULL, last_login_at DATETIME);
           CREATE TABLE courses (id INTEGER PRIMARY KEY, code VARCHAR(40) NOT NULL, title VARCHAR(200) NOT NULL,
             term VARCHAR(60) NOT NULL, description TEXT NOT NULL, archived BOOLEAN NOT NULL, created_at DATETIME NOT NULL);
           CREATE TABLE settings (key VARCHAR(80) PRIMARY KEY, value TEXT NOT NULL);
           INSERT INTO settings VALUES ('schema_version', '2');
           INSERT INTO users VALUES (1, 'old', 'Old', '', '', 'admin', 'local', NULL, 0, 1, '2026-01-01', NULL);"""
    )
    con.close()
    app_ = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}"})
    with app_.app_context():
        assert upgrade_schema() == 2
        user = db.session.get(User, 1)
        user.external_id = "rosario:staff:1"
        db.session.commit()
        assert upgrade_schema() == SCHEMA_VERSION  # idempotent
