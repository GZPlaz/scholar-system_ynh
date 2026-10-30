"""Lockdown browser (Safe Exam Browser) and the Pear-style test security settings."""
import hashlib
import plistlib
from datetime import timedelta

from test_app import app, login  # noqa: F401  (fixture re-export; puts sources/ on sys.path)
from test_portal import UA, answers, begin, demo, sign_in

from scholar.extensions import db
from scholar.lockdown import config_key, hash_quit_password, seb_json, seb_settings
from scholar.models import Accommodation, Assessment, Attempt, AttemptEvent, TestSession, User, utcnow
from scholar.security import check_access_password, rotating_password

BASE = "http://localhost"
WINDOWS_SEB = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) SEB/3.7"}


def seb_key(app_, aid):
    with app_.app_context(), app_.test_request_context():
        a = db.session.get(Assessment, aid)
        return config_key(seb_settings(a, BASE + "/", BASE + "/seb/quit"))


def seb_headers(path, key, ua=WINDOWS_SEB):
    """What SEB for Windows sends: SHA256(absolute URL + Config Key)."""
    return {**ua, "X-SafeExamBrowser-ConfigKeyHash": hashlib.sha256((BASE + path + key).encode()).hexdigest()}


# --------------------------------------------------------------------------- the config key


def test_seb_json_follows_the_config_key_rules():
    settings = {"originatorVersion": "x", "startURL": "https://a.example/", "allowQuit": True, "URLFilterEnable": False,
                "browserViewMode": 1, "nested": {"b": 1, "A": [1, {"z": True, "Y": "s\\t"}]}, "empty": {}}
    text = seb_json(settings)
    assert " " not in text and "originatorVersion" not in text and "empty" not in text
    # case-insensitive key order, nested dicts too; slashes and backslashes are not escaped
    assert text == ('{"allowQuit":true,"browserViewMode":1,"nested":{"A":[1,{"Y":"s\\t","z":true}],"b":1},'
                    '"startURL":"https://a.example/","URLFilterEnable":false}')
    assert config_key(settings) == hashlib.sha256(text.encode()).hexdigest()


def test_seb_config_file(app):
    aid, code = demo(app)
    c = app.test_client()
    assert c.get(f"/seb/{code}.seb").status_code == 404  # not required: no file
    with app.app_context():
        db.session.get(Assessment, aid).lockdown_browser = "seb"
        db.session.get(Assessment, aid).seb_quit_password_hash = hash_quit_password("proctor-quit-1")
        db.session.commit()
    r = c.get(f"/seb/{code.lower()}.seb")
    assert r.status_code == 200 and r.mimetype == "application/seb"
    settings = plistlib.loads(r.data)
    assert settings["startURL"] == BASE + "/" and settings["sendBrowserExamKey"] is True
    assert settings["quitURL"] == BASE + "/seb/quit" and settings["browserViewMode"] == 1
    assert settings["hashedQuitPassword"] == hashlib.sha256(b"proctor-quit-1").hexdigest()
    # the key Scholar checks is the key of exactly this file
    assert config_key(settings) == seb_key(app, aid)


# --------------------------------------------------------------------------- requiring SEB


def test_ordinary_browsers_are_sent_to_the_lockdown_page(app):
    aid, code = demo(app, lockdown_browser="seb")
    c = app.test_client()
    r = c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=UA)
    assert r.headers["Location"].endswith("/lockdown")
    r = c.get("/lockdown", headers=UA)
    assert b"Open this test in Safe Exam Browser" in r.data and b"sebs://" not in r.data  # http test server
    assert f"seb://localhost/seb/{code}.seb".encode() in r.data
    for page in ("/verify", "/start", "/attempt"):
        assert c.get(page, headers=UA).headers["Location"].endswith(("/lockdown", "/"))
    # nothing else of the test is reachable either
    assert c.post("/verify", data={"answer": "yes"}, headers=UA).headers["Location"].endswith("/lockdown")


def test_seb_for_windows_headers(app):
    aid, code = demo(app, lockdown_browser="seb")
    key = seb_key(app, aid)
    c = app.test_client()
    r = c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=seb_headers("/", key))
    assert r.headers["Location"].endswith("/verify")
    r = c.post("/verify", data={"answer": "yes"}, headers=seb_headers("/verify", key))
    assert r.headers["Location"].endswith("/start")
    r = c.post("/start", headers=seb_headers("/start", key))
    assert r.headers["Location"].endswith("/attempt")
    assert c.get("/attempt", headers=seb_headers("/attempt", key)).status_code == 200
    # a header for another URL (replayed) or another configuration is refused, and stays refused
    r = c.get("/attempt", headers=seb_headers("/start", key))
    assert r.headers["Location"].endswith("/lockdown")
    assert c.get("/attempt", headers=WINDOWS_SEB).headers["Location"].endswith("/lockdown")
    with app.app_context():
        assert "lockdown_refused" in [e.kind for e in Attempt.query.filter_by(status="in_progress").one().events]


def test_seb_for_mac_and_ipad_javascript_api(app):
    aid, code = demo(app, lockdown_browser="seb")
    key = seb_key(app, aid)
    mac = {"User-Agent": "Mozilla/5.0 (Macintosh) SEB/3.4"}
    c = app.test_client()
    c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=mac)
    page = BASE + "/lockdown"
    good = hashlib.sha256((page + key).encode()).hexdigest()
    assert c.post("/lockdown/check", json={"url": page, "config_key": "0" * 64}, headers=mac).status_code == 403
    assert c.post("/lockdown/check", json={"url": BASE + "/", "config_key": good}, headers=mac).status_code == 403
    r = c.post("/lockdown/check", json={"url": page + "#x", "config_key": good, "version": "SEB_macOS_3.4"}, headers=mac)
    assert r.status_code == 200 and r.json["redirect"].endswith("/verify")
    # no headers from now on (WKWebView can't send them): the verified session carries on
    assert c.post("/verify", data={"answer": "yes"}, headers=mac).headers["Location"].endswith("/start")
    assert c.post("/start", headers=mac).headers["Location"].endswith("/attempt")
    assert c.post("/attempt/save", data={}, headers=mac).json["ok"] is True
    # the session can't be carried to an ordinary browser
    assert c.get("/attempt", headers=UA).status_code == 302


def test_teacher_supplied_browser_exam_key(app):
    bek = "ab" * 32
    aid, code = demo(app, lockdown_browser="seb", seb_extra_keys=f"# school laptops\n{bek}")
    c = app.test_client()
    header = hashlib.sha256((BASE + "/" + bek).encode()).hexdigest()
    r = c.post("/", data={"code": code, "student_id": "MA2026008"}, headers={**WINDOWS_SEB, "X-SafeExamBrowser-RequestHash": header})
    assert r.headers["Location"].endswith("/verify")
    # strict mode: the generated settings key alone no longer opens the test
    demo(app, seb_only_listed_keys=True)
    key = seb_key(app, aid)
    strict = app.test_client()
    r = strict.post("/", data={"code": code, "student_id": "MA2026008"}, headers=seb_headers("/", key))
    assert r.headers["Location"].endswith("/lockdown")


# --------------------------------------------------------------------------- passwords


def test_static_and_rotating_start_passwords(app):
    aid, code = demo(app, access_password_mode="static", access_password="Blue Fox")
    c = app.test_client()
    sign_in(c, code)
    assert b"Test password" in c.get("/start", headers=UA).data
    assert c.post("/start", data={}, headers=UA).status_code == 401
    assert c.post("/start", data={"password": "red fox"}, headers=UA).status_code == 401
    assert c.post("/start", data={"password": " blue-fox "}, headers=UA).headers["Location"].endswith("/attempt")
    with app.app_context():
        a = db.session.get(Assessment, aid)
        a.access_password_mode, a.password_rotation_minutes = "rotating", 5
        db.session.commit()
        now = utcnow()
        current, _ = rotating_password(a, now)
        previous, _ = rotating_password(a, now - timedelta(minutes=5))
        stale, _ = rotating_password(a, now - timedelta(minutes=10))
        assert check_access_password(a, current, now) and check_access_password(a, previous, now)
        assert stale == current or not check_access_password(a, stale, now)
        assert len(current) == 6
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    assert b"Password right now" in staff.get(f"/staff/assessments/{aid}/monitor").data


def test_proctor_override_replaces_a_missing_pin(app):
    from werkzeug.security import generate_password_hash

    aid, code = demo(app, identity_check="pin", bypass_password_hash=generate_password_hash("proctor-123"))
    c = app.test_client()
    c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=UA)
    assert b"Proctor override" in c.get("/verify", headers=UA).data
    assert c.post("/verify", data={"answer": "yes", "bypass": "nope-nope"}, headers=UA).status_code == 401
    r = c.post("/verify", data={"answer": "yes", "bypass": "proctor-123"}, headers=UA)
    assert r.headers["Location"].endswith("/start")


# --------------------------------------------------------------------------- one sitting, navigation


def test_one_sitting_pauses_a_student_who_left(app):
    aid, code = demo(app, one_sitting=True, lock_to_device=False)
    first = app.test_client()
    sign_in(first, code)
    attempt_id = begin(app, first)
    # signing in again (closed the browser, another tab...) pauses the test
    second = app.test_client()
    sign_in(second, code, headers={"User-Agent": "Other"})
    second.post("/start", headers={"User-Agent": "Other"})
    assert b"Your test is paused" in second.get("/attempt", headers={"User-Agent": "Other"}).data
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.locked and "before submitting" in attempt.lock_reason
        uid = attempt.user_id
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/unlock")
    assert b'id="take-form"' in second.get("/attempt", headers={"User-Agent": "Other"}).data
    # a long silence also counts as leaving
    with app.app_context():
        session = TestSession.query.filter_by(status="ready", user_agent="Other").one()
        session.last_seen_at = utcnow() - timedelta(minutes=5)
        db.session.commit()
    assert b"Your test is paused" in second.get("/attempt", headers={"User-Agent": "Other"}).data
    # moving a student to another device on purpose doesn't pause them
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/unlock")
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/reset_device")
    third = app.test_client()
    sign_in(third, code, headers={"User-Agent": "Third"})
    with app.app_context():
        assert not db.session.get(Attempt, attempt_id).locked


def test_restricted_navigation_is_enforced_by_the_server(app):
    aid, code = demo(app, sequential=True)
    c = app.test_client()
    sign_in(c, code)
    attempt_id = begin(app, c)
    form = answers(app, attempt_id)
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        from scholar.taking import ordered_entries

        order = [e["item"].id for e in ordered_entries(attempt)]
    r = c.get("/attempt", headers=UA)
    assert b"Question 1 of 10" in r.data and r.data.count(b'class="question"') == 1
    assert f'name="present_{order[1]}"'.encode() not in r.data
    # answers to other questions are ignored
    c.post("/attempt/save", data=form, headers=UA)
    with app.app_context():
        saved = {r.item_id for r in db.session.get(Attempt, attempt_id).responses}
        assert saved == {order[0]}
    c.post("/attempt/next", data=form, headers=UA)
    assert b"Question 2 of 10" in c.get("/attempt", headers=UA).data
    # there's no way back: question 1 can't be changed any more
    c.post("/attempt/save", data={f"q_{order[0]}": "999", f"present_{order[0]}": "1"}, headers=UA)
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.position == 1 and attempt.response_map()[order[0]].get_answer() != "999"


# --------------------------------------------------------------------------- leaving the page, pausing, accommodations


def test_short_absences_under_the_grace_period_are_ignored(app):
    aid, code = demo(app, focus_grace_seconds=5, max_focus_losses=2)
    c = app.test_client()
    sign_in(c, code)
    attempt_id = begin(app, c)
    c.post("/attempt/event", data={"kind": "focus_lost"}, headers=UA)
    r = c.post("/attempt/event", data={"kind": "focus_returned"}, headers=UA)
    assert r.json["warning"] is None
    with app.app_context():
        assert db.session.get(Attempt, attempt_id).focus_losses == 0

    def leave_for(seconds):
        """A new exit, `seconds` ago (earlier events moved further back, as in real time)."""
        with app.app_context():
            for e in AttemptEvent.query.filter_by(attempt_id=attempt_id):
                e.created_at -= timedelta(seconds=120)
            db.session.commit()
        c.post("/attempt/event", data={"kind": "focus_lost"}, headers=UA)
        with app.app_context():
            for e in AttemptEvent.query.filter_by(attempt_id=attempt_id, detail="pending"):
                e.created_at -= timedelta(seconds=seconds)
            db.session.commit()

    leave_for(8)
    r = c.post("/attempt/event", data={"kind": "focus_returned"}, headers=UA)
    assert r.json["warning"] == {"count": 1, "limit": 2} and not r.json["locked"]
    # an exit whose return never arrives is counted once the grace period is clearly over
    leave_for(60)
    r = c.get("/attempt/status", headers=UA)
    assert r.json["locked"] is True


def test_stopping_the_clock_while_paused(app):
    aid, code = demo(app, clock_stops_when_paused=True, time_limit_minutes=30)
    c = app.test_client()
    sign_in(c, code)
    attempt_id = begin(app, c)
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    with app.app_context():
        uid = db.session.get(Attempt, attempt_id).user_id
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/lock")
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        attempt.locked_at -= timedelta(minutes=3)
        db.session.commit()
    r = staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/unlock", follow_redirects=True)
    assert b"paused minute(s) were given back" in r.data
    with app.app_context():
        assert db.session.get(Attempt, attempt_id).extra_minutes >= 3


def test_accommodations_extra_time_and_attempts(app):
    aid, code = demo(app, max_attempts=1, time_limit_minutes=20)
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    with app.app_context():
        uid = User.query.filter_by(username="demo.student1").one().id  # already has an attempt
    c = app.test_client()
    sign_in(c, code, "MA2026001")
    assert b"used all of your attempts" in c.get("/start", headers=UA).data
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/allow_attempt")
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/extra_time", data={"minutes": "10"})
    r = c.get("/start", headers=UA)
    assert b"30 minutes (including 10 extra for you)" in r.data
    attempt_id = begin(app, c)
    with app.app_context():
        assert db.session.get(Attempt, attempt_id).extra_minutes == 10
        assert Accommodation.query.filter_by(user_id=uid).one().extra_attempts == 1


def test_settings_form_for_lockdown_and_pear_options(app):
    aid, code = demo(app)
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    form = {
        "title": "Unit 1 Check-in", "max_attempts": "2", "show_results": "immediately", "allow_online": "1",
        "identity_check": "confirm", "lockdown_browser": "seb", "seb_quit_password": "quit-it-please",
        "seb_extra_keys": "# lab\n" + "c" * 64, "access_password_mode": "rotating", "password_rotation_minutes": "3",
        "bypass_password": "override-123", "one_sitting": "1", "sequential": "1", "focus_grace_seconds": "5",
        "clock_stops_when_paused": "1",
    }
    r = staff.post(f"/staff/assessments/{aid}/settings", data=form, follow_redirects=True)
    assert b"Settings saved" in r.data
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert a.lockdown_browser == "seb" and a.seb_quit_password_hash == hashlib.sha256(b"quit-it-please").hexdigest()
        assert a.access_password_mode == "rotating" and a.password_rotation_minutes == 3 and a.password_secret
        assert a.bypass_password_hash and a.one_sitting and a.sequential and a.focus_grace_seconds == 5 and a.clock_stops_when_paused
    bad = dict(form, seb_extra_keys="not-a-key", seb_quit_password="short", access_password_mode="static",
               access_password="ab", password_rotation_minutes="0", bypass_password="x", focus_grace_seconds="99")
    r = staff.post(f"/staff/assessments/{aid}/settings", data=bad)
    for text in (b"not a Safe Exam Browser key", b"quit password needs", b"at least 4 letters", b"1 to 60 minutes", b"override password needs", b"0 to 60 seconds"):
        assert text in r.data, text
    # the page links the settings file for proctors
    assert f"/seb/{code}.seb".encode() in staff.get(f"/staff/assessments/{aid}/settings").data
