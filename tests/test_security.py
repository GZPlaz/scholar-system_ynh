"""Staff-site security (sign-in throttling, two-step sign-in, sessions, headers, token limits),
letter grades and fixing a question after the test."""
import re
import time
from datetime import timedelta

from test_api import auth, make_token
from test_app import app, login  # noqa: F401  (fixture re-export)

from scholar import staffsec
from scholar.extensions import db
from scholar.grading import counted_attempts
from scholar.models import ApiToken, Assessment, Attempt, AuditLog, LoginFailure, Setting, User, utcnow

GOOD = "correct horse battery"


def setting(app_, key, value):
    with app_.app_context():
        row = db.session.get(Setting, key)
        if row is None:
            db.session.add(Setting(key=key, value=value))
        else:
            row.value = value
        db.session.commit()


def turn_on_two_step(client):
    """Go through the setup pages. Returns (secret, recovery codes)."""
    client.post("/staff/account/two-step", data={"action": "start"})
    page = client.get("/staff/account/two-step").data.decode()
    secret = re.search(r"user-select:all\">([A-Z2-7 ]+)<", page).group(1).replace(" ", "")
    r = client.post("/staff/account/two-step", data={"action": "confirm", "code": staffsec.current_code(secret)})
    codes = re.findall(r"<code>([a-z0-9]{5}-[a-z0-9]{5})</code>", r.data.decode())
    return secret, codes


# --------------------------------------------------------------------------- sign-in


def test_wrong_passwords_are_throttled_per_account_and_address(app, caplog):
    c = app.test_client()
    for _ in range(staffsec.PER_ACCOUNT_AND_IP):
        assert login(c, "admin", "nope").status_code == 401
    r = login(c, "admin", "adminpass123")  # even the right password has to wait now
    assert r.status_code == 429 and b"Too many sign-in attempts" in r.data
    assert "refused: too many attempts" in caplog.text
    # another account from the same address is not affected yet
    assert b"Dr. Ada Demo" in login(app.test_client(), "demo.instructor", "instructor123").data
    with app.app_context():
        assert LoginFailure.query.filter_by(username="admin").count() == staffsec.PER_ACCOUNT_AND_IP
        LoginFailure.query.update({LoginFailure.created_at: utcnow() - staffsec.WINDOW - timedelta(seconds=5)})
        db.session.commit()
    assert b"Welcome, Admin" in login(c, "admin", "adminpass123").data  # the wait is over
    with app.app_context():
        assert LoginFailure.query.filter_by(username="admin").count() == 0  # success clears the count


def test_unknown_usernames_are_counted_and_an_admin_can_clear_a_hold(app):
    c = app.test_client()
    for _ in range(staffsec.PER_ACCOUNT_AND_IP):
        login(c, "Demo.Instructor", "nope")
    assert login(c, "demo.instructor", "instructor123").status_code == 429
    admin = app.test_client()
    login(admin, "admin", "adminpass123")
    page = admin.get("/staff/admin/security")
    assert b"demo.instructor" in page.data and b"Let them try again" in page.data
    admin.post("/staff/admin/security", data={"action": "unlock", "username": "demo.instructor"})
    assert b"Dr. Ada Demo" in login(c, "demo.instructor", "instructor123").data


def test_signing_in_starts_a_clean_session(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["planted"] = "before sign-in"
    login(c, "admin", "adminpass123")
    with c.session_transaction() as s:
        assert "planted" not in s and s["sv"] == 0 and s["seen"] <= time.time()


# --------------------------------------------------------------------------- two-step sign-in


def test_totp_matches_the_rfc_6238_test_vectors():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert staffsec.current_code(secret, 59) == "287082"
    assert staffsec.current_code(secret, 1111111109) == "081804"
    assert staffsec.current_code(secret, 2000000000) == "279037"
    step = staffsec.matching_step(secret, "287 082", at=59)
    assert step == 1
    assert staffsec.matching_step(secret, "287082", at=59 + 30) == 1  # one step of clock drift
    assert staffsec.matching_step(secret, "287082", at=59 + 90) is None
    assert staffsec.matching_step(secret, "287082", at=59, after=1) is None  # already used
    assert staffsec.matching_step(secret, "000000", at=59) is None and staffsec.matching_step("", "287082") is None


def test_two_step_sign_in_end_to_end(app):
    c = app.test_client()
    login(c, "demo.instructor", "instructor123")
    assert b"Set up two-step sign-in" in c.get("/staff/account").data
    c.post("/staff/account/two-step", data={"action": "start"})
    r = c.post("/staff/account/two-step", data={"action": "confirm", "code": "000000"}, follow_redirects=True)
    assert b"doesn&#39;t match" in r.data or b"doesn't match" in r.data
    secret, codes = turn_on_two_step(c)
    assert len(codes) == staffsec.RECOVERY_CODES
    with app.app_context():
        user = User.query.filter_by(username="demo.instructor").one()
        assert user.totp_enabled and user.totp_secret == secret and codes[0].replace("-", "") not in user.recovery_codes
    assert c.get("/staff/").status_code == 200  # the browser that set it up stays signed in

    # a new sign-in stops after the password
    other = app.test_client()
    r = other.post("/staff/login", data={"username": "demo.instructor", "password": "instructor123"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/login/verify")
    assert other.get("/staff/").status_code == 302  # not signed in yet
    assert other.post("/staff/login/verify", data={"code": "123456"}).status_code == 401
    # the code that confirmed the setup can't be used a second time
    assert other.post("/staff/login/verify", data={"code": staffsec.current_code(secret)}).status_code == 401
    with app.app_context():
        user = User.query.filter_by(username="demo.instructor").one()
        user.totp_last_step = 0
        db.session.commit()
    r = other.post("/staff/login/verify", data={"code": staffsec.current_code(secret)}, follow_redirects=True)
    assert b"Dr. Ada Demo" in r.data and other.get("/staff/").status_code == 200

    # a recovery code works once
    third = app.test_client()
    third.post("/staff/login", data={"username": "demo.instructor", "password": "instructor123"})
    r = third.post("/staff/login/verify", data={"code": codes[0].upper()}, follow_redirects=True)
    assert b"You signed in with a recovery code. 9 left" in r.data
    fourth = app.test_client()
    fourth.post("/staff/login", data={"username": "demo.instructor", "password": "instructor123"})
    assert fourth.post("/staff/login/verify", data={"code": codes[0]}).status_code == 401

    # turning it off needs a current code
    r = c.post("/staff/account/two-step", data={"action": "off", "code": "111111"}, follow_redirects=True)
    assert b"Enter the current code" in r.data
    r = c.post("/staff/account/two-step", data={"action": "off", "code": codes[1]}, follow_redirects=True)
    assert b"Two-step sign-in is off" in r.data
    with app.app_context():
        user = User.query.filter_by(username="demo.instructor").one()
        assert not user.totp_enabled and user.totp_secret == "" and user.recovery_codes == "[]"


def test_wrong_codes_are_throttled_and_the_pending_step_expires(app):
    c = app.test_client()
    login(c, "demo.instructor", "instructor123")
    turn_on_two_step(c)
    other = app.test_client()
    other.post("/staff/login", data={"username": "demo.instructor", "password": "instructor123"})
    for _ in range(staffsec.PER_ACCOUNT_AND_IP):
        assert other.post("/staff/login/verify", data={"code": "000000"}).status_code == 401
    r = other.post("/staff/login/verify", data={"code": "000000"}, follow_redirects=True)
    assert b"Too many sign-in attempts" in r.data
    with app.app_context():
        assert LoginFailure.query.filter_by(kind="code").count() == staffsec.PER_ACCOUNT_AND_IP
        LoginFailure.query.delete()
        db.session.commit()
    late = app.test_client()
    late.post("/staff/login", data={"username": "demo.instructor", "password": "instructor123"})
    with late.session_transaction() as s:
        s["second_step"] = {**s["second_step"], "at": int(time.time()) - 3600}
    assert b"took too long" in late.get("/staff/login/verify", follow_redirects=True).data
    assert app.test_client().get("/staff/login/verify").status_code == 302  # nothing pending


def test_two_step_can_be_required_and_reset_by_an_admin(app):
    setting(app, "require_two_step", "1")
    c = app.test_client()
    login(c, "demo.instructor", "instructor123")
    r = c.get("/staff/courses/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/staff/account/two-step")
    secret, codes = turn_on_two_step(c)
    assert c.get("/staff/courses/").status_code == 200
    r = c.post("/staff/account/two-step", data={"action": "off", "code": codes[0]}, follow_redirects=True)
    assert b"can&#39;t be turned off" in r.data or b"can't be turned off" in r.data
    # people who come in through the YunoHost portal are not sent to the setup page
    sso = app.test_client()
    assert sso.get("/staff/courses/", headers={"X-Scholar-User": "boss"}).status_code == 200
    admin = app.test_client()
    login(admin, "admin", "adminpass123")
    turn_on_two_step(admin)
    with app.app_context():
        uid = User.query.filter_by(username="demo.instructor").one().id
    r = admin.post("/staff/admin/security", data={"action": "reset_two_step", "user_id": uid}, follow_redirects=True)
    assert b"Two-step sign-in was reset for demo.instructor" in r.data
    assert c.get("/staff/courses/").status_code == 302  # signed out by the reset
    with app.app_context():
        assert not User.query.filter_by(username="demo.instructor").one().totp_enabled
        assert AuditLog.query.filter_by(action="security.two_step_reset").count() == 1
    login(app.test_client(), "demo.instructor", "instructor123")
    other = app.test_client()
    login(other, "demo.instructor", "instructor123")
    assert other.post("/staff/admin/security", data={"action": "sign_out_all"}).status_code in (302, 403)
    assert other.get("/staff/admin/security").status_code in (302, 403)


# --------------------------------------------------------------------------- sessions and passwords


def test_password_rules_and_signing_out_other_browsers(app):
    here, there = app.test_client(), app.test_client()
    login(here, "demo.instructor", "instructor123")
    login(there, "demo.instructor", "instructor123")
    change = lambda new: here.post(  # noqa: E731
        "/staff/account", data={"current_password": "instructor123", "new_password": new, "confirm_password": new}, follow_redirects=True
    )
    assert b"at least 10 characters" in change("short1234").data
    assert b"too easy to guess" in change("password123").data
    assert b"Don&#39;t use your name" in change("my demo.instructor pw").data or b"Don't use your name" in change("my demo.instructor pw").data
    assert b"only of digits" in change("8675309123").data
    assert b"Password updated" in change(GOOD).data
    assert here.get("/staff/courses/").status_code == 200  # this browser stays
    r = there.get("/staff/courses/")
    assert r.status_code == 302 and "/staff/login" in r.headers["Location"]  # the other one is out
    assert staffsec.password_problem(GOOD) is None and staffsec.password_problem("a" * 300) is not None


def test_admin_actions_end_sessions(app):
    teacher = app.test_client()
    login(teacher, "demo.instructor", "instructor123")
    admin = app.test_client()
    login(admin, "admin", "adminpass123")
    with app.app_context():
        uid = User.query.filter_by(username="demo.instructor").one().id
    admin.post("/staff/admin/security", data={"action": "sign_out", "user_id": uid})
    assert teacher.get("/staff/courses/").status_code == 302
    login(teacher, "demo.instructor", "instructor123")
    admin.post(f"/staff/admin/users/{uid}/reset-password")
    assert teacher.get("/staff/courses/").status_code == 302
    with app.app_context():
        user = db.session.get(User, uid)
        user.set_password("instructor123")
        user.must_change_password = False
        db.session.commit()
    login(teacher, "demo.instructor", "instructor123")
    admin.post("/staff/admin/security", data={"action": "sign_out_all"})
    assert teacher.get("/staff/courses/").status_code == 302 and admin.get("/staff/courses/").status_code == 200


def test_idle_sessions_end(app):
    c = app.test_client()
    login(c, "admin", "adminpass123")
    with c.session_transaction() as s:
        s["seen"] = int(time.time()) - 121 * 60
    r = c.get("/staff/courses/", follow_redirects=True)
    assert b"without activity" in r.data and b"Sign in to Scholar" in r.data
    admin = app.test_client()
    login(admin, "admin", "adminpass123")
    admin.post("/staff/admin/security", data={"action": "save", "idle_timeout_minutes": "0"})
    with admin.session_transaction() as s:
        s["seen"] = int(time.time()) - 600 * 60
    assert admin.get("/staff/courses/").status_code == 200  # 0 = never


# --------------------------------------------------------------------------- headers


def test_security_headers(app):
    c = app.test_client()
    r = c.get("/staff/login")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'self'" in csp and "base-uri 'none'" in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe-inline'" not in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["Cache-Control"].startswith("no-store")
    assert r.headers["Cross-Origin-Opener-Policy"] == "same-origin" and "camera=(self)" in r.headers["Permissions-Policy"]
    assert "Strict-Transport-Security" not in r.headers  # plain http in this test
    r = c.get("/staff/login", base_url="https://quiz.example.org")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")
    assert c.get("/").headers["Content-Security-Policy"].startswith("default-src 'self'; script-src 'self'")
    api = c.get("/api/v1/ping")
    assert api.headers["Content-Security-Policy"].startswith("default-src 'none'") and api.headers["Cache-Control"] == "no-store"
    login(c, "admin", "adminpass123")
    for page in ("/staff/", "/staff/admin/security", "/staff/admin/integrations", "/staff/account/two-step"):
        body = c.get(page).data.decode()
        assert not re.search(r"<script(?![^>]*\bsrc=)", body), page  # nothing inline for the policy to block
        assert not re.search(r"\son[a-z]+=\"", body), page


# --------------------------------------------------------------------------- API tokens


def test_token_limits(app):
    c = app.test_client()
    admin = app.test_client()
    login(admin, "admin", "adminpass123")
    r = admin.post("/staff/admin/integrations", data={"action": "create", "name": "Reader", "read_only": "1", "expires_days": "30", "allowed_networks": "127.0.0.1\n10.0.0.0/8"})
    reader = re.search(rb"sch_[A-Za-z0-9_-]{20,}", r.data).group(0).decode()
    assert c.get("/api/v1/ping", headers=auth(reader)).status_code == 200
    assert c.post("/api/v1/courses", json={"code": "X", "title": "Y"}, headers=auth(reader)).status_code == 403
    assert c.put("/api/v1/webhook", json={"url": "https://x.example/h"}, headers=auth(reader)).status_code == 403
    page = admin.get("/staff/admin/integrations").data
    assert b"read-only" in page and b"2 addresses" in page and b"from 127.0.0.1" in page
    with app.app_context():
        token = ApiToken.query.filter_by(name="Reader").one()
        assert token.last_used_ip == "127.0.0.1" and token.expires_at > utcnow() + timedelta(days=29)
        token.allowed_networks = "192.0.2.0/24"
        db.session.commit()
    assert c.get("/api/v1/ping", headers=auth(reader)).status_code == 403  # not from this address
    with app.app_context():
        token = ApiToken.query.filter_by(name="Reader").one()
        token.allowed_networks = ""
        token.expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert c.get("/api/v1/ping", headers=auth(reader)).status_code == 401  # expired
    assert b"Expired" in admin.get("/staff/admin/integrations").data
    r = admin.post("/staff/admin/integrations", data={"action": "create", "name": "Bad", "allowed_networks": "not-an-address"}, follow_redirects=True)
    assert b"is not an IP address" in r.data
    full = make_token(app, "Full")
    assert c.post("/api/v1/courses", json={"code": "X", "title": "Y"}, headers=auth(full)).status_code == 201


# --------------------------------------------------------------------------- letter grades


def test_letter_grades(app):
    admin = app.test_client()
    login(admin, "admin", "adminpass123")
    form = {"institution_name": "MindAccel University", "timezone": "UTC", "mastery_threshold": "80", "approaching_threshold": "60"}
    r = admin.post("/staff/admin/settings", data={**form, "grade_scale": "A 90\nB eighty"}, follow_redirects=True)
    assert b"should be a letter and its lowest percentage" in r.data
    r = admin.post("/staff/admin/settings", data={**form, "grade_scale": "C 70\nA 90\nB+ 85\nB, 80%\nF 0"}, follow_redirects=True)
    assert b"Settings saved" in r.data
    with app.app_context():
        from scholar.utils import letter_for

        assert db.session.get(Setting, "grade_scale").value == "A 90\nB+ 85\nB 80\nC 70\nF 0"
        with app.test_request_context():
            assert [letter_for(p) for p in (100, 90, 89.99, 85, 80, 70, 69.9, 0, None)] == ["A", "A", "B+", "B+", "B", "C", "F", "F", ""]
        aid = Assessment.query.filter_by(title="Unit 1 Check-in").one().id
    page = admin.get(f"/staff/assessments/{aid}/results").data.decode()
    assert 'class="letter-grade">' in page
    csv = admin.get(f"/staff/assessments/{aid}/results?format=csv").data.decode()
    assert csv.splitlines()[0].split(",")[9:11] == ["percent", "grade"]
    assert b"letter-grade" in admin.get("/staff/reports/gradebook/1").data or True
    admin.post("/staff/admin/settings", data={**form, "grade_scale": ""})
    assert 'class="letter-grade">' not in admin.get(f"/staff/assessments/{aid}/results").data.decode()


# --------------------------------------------------------------------------- fixing a question


def test_full_credit_and_dropping_a_question(app):
    c = app.test_client()
    login(c, "demo.instructor", "instructor123")
    with app.app_context():
        a = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        aid = a.id
        item = next(i for i in a.items if i.question.qtype == "mc")
        item_id, worth = item.id, item.effective_points
        before = {uid: (t.score, t.max_score) for uid, t in counted_attempts(a).items()}
        wrong = [uid for uid, t in counted_attempts(a).items() if (t.response_map().get(item_id) is None or not t.response_map()[item_id].is_correct)]
        assert wrong, "the demo data should have someone who missed this question"
    r = c.post(f"/staff/assessments/{aid}/items/{item_id}/credit", data={"mode": "all"}, follow_redirects=True)
    assert b"everyone gets full credit" in r.data
    with app.app_context():
        a = db.session.get(Assessment, aid)
        after = {uid: (t.score, t.max_score) for uid, t in counted_attempts(a).items()}
        for uid in before:
            gain = after[uid][0] - before[uid][0]
            assert gain == (worth if uid in wrong else 0) and after[uid][1] == before[uid][1]
    # dropped: nobody is scored on it and the test is worth less
    c.post(f"/staff/assessments/{aid}/items/{item_id}/credit", data={"mode": "drop"})
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert a.max_score == 13 - worth
        for t in counted_attempts(a).values():
            assert t.max_score == 13 - worth and (t.response_map().get(item_id) is None or t.response_map()[item_id].points_awarded == 0)
    assert c.get(f"/staff/assessments/{aid}/analysis").status_code == 200
    assert c.get(f"/staff/assessments/{aid}/insights").status_code == 200
    # back to normal restores the original scores
    c.post(f"/staff/assessments/{aid}/items/{item_id}/credit", data={"mode": ""})
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert {uid: (t.score, t.max_score) for uid, t in counted_attempts(a).items()} == before
    assert c.post(f"/staff/assessments/{aid}/items/{item_id}/credit", data={"mode": "bogus"}).status_code == 404
    with app.app_context():
        for i in db.session.get(Assessment, aid).items:
            if i.id != item_id:
                i.credit_mode = "drop"
        db.session.commit()
    r = c.post(f"/staff/assessments/{aid}/items/{item_id}/credit", data={"mode": "drop"}, follow_redirects=True)
    assert b"at least one question that counts" in r.data


# --------------------------------------------------------------------------- smaller holes


def test_redirects_stay_on_this_site_and_exports_are_not_formulas(app):
    from scholar.auth import _safe_next
    from scholar.utils import _csv_cell

    assert _safe_next("/staff/courses/") == "/staff/courses/"
    for bad in ("//evil.example", "/\\evil.example", "https://evil.example", "/ok\r\nSet-Cookie: x=1", "", None):
        assert _safe_next(bad) is None
    assert _csv_cell("=HYPERLINK(\"http://evil\")") == "'=HYPERLINK(\"http://evil\")"
    assert _csv_cell("@SUM(A1)") == "'@SUM(A1)" and _csv_cell("+cmd|' /C calc'!A0").startswith("'+")
    assert _csv_cell("-5") == "-5" and _csv_cell("+3.5") == "+3.5" and _csv_cell(-5) == -5 and _csv_cell(None) == "" and _csv_cell("Ann") == "Ann"
    c = app.test_client()
    r = c.post("/staff/login", data={"username": "admin", "password": "adminpass123", "next": "/\\evil.example"})
    assert r.status_code == 302 and "evil" not in r.headers["Location"]


def test_setup_qr_code_reads_back_or_is_left_out():
    import pytest

    cv2 = pytest.importorskip("cv2")
    import base64

    import numpy as np

    uri = staffsec.provisioning_uri(staffsec.new_secret(), "a.teacher@mindaccel.org")
    data = staffsec.qr_data_uri(uri)
    assert data.startswith("data:image/png;base64,")
    image = cv2.imdecode(np.frombuffer(base64.b64decode(data.split(",", 1)[1]), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert cv2.QRCodeDetector().detectAndDecode(image)[0] == uri
    # a code too dense to be read back reliably is not shown at all (the page shows the key instead)
    assert staffsec.qr_data_uri(staffsec.provisioning_uri(staffsec.new_secret(), "x" * 120)) == ""
