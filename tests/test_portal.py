"""The student test portal (/), its security rules, proctoring and per-test analytics."""
import re
from datetime import timedelta

from test_app import app, login  # noqa: F401  (fixture re-export; puts sources/ on sys.path)

from scholar import SCHEMA_VERSION, upgrade_schema
from scholar.analytics import answer_similarity, assessment_insights
from scholar.extensions import db
from scholar.models import Assessment, Attempt, AttemptEvent, Setting, TestSession, User, utcnow
from scholar.security import PORTAL_COOKIE

UA = {"User-Agent": "Mozilla/5.0 (Exam laptop)"}


def demo(app_, **settings):
    """The demo assessment (with optional setting changes) → (assessment id, test code)."""
    with app_.app_context():
        a = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        for key, value in settings.items():
            setattr(a, key, value)
        db.session.commit()
        return a.id, a.access_code


def sign_in(client, code, student_id="MA2026008", headers=UA):
    """Code and ID, then 'yes, this is me'. Returns the last response."""
    r = client.post("/", data={"code": code, "student_id": student_id}, headers=headers)
    if r.status_code != 302:
        return r
    return client.post("/verify", data={"answer": "yes"}, headers=headers)


def answers(app_, attempt_id):
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
            elif q.qtype == "short":
                form[f"q_{item.id}"] = "  ZERO "
            elif q.qtype == "numeric":
                form[f"q_{item.id}"] = "5.0"
            else:
                form[f"q_{item.id}"] = "Because composition."
        return form


def begin(app_, client):
    r = client.post("/start", headers=UA)
    assert r.status_code == 302 and r.headers["Location"].endswith("/attempt"), r.data[:300]
    with app_.app_context():
        return Attempt.query.filter_by(status="in_progress").order_by(Attempt.id.desc()).first().id


# --------------------------------------------------------------------------- flow


def test_full_portal_flow(app):
    aid, code = demo(app, time_limit_minutes=30)
    c = app.test_client()
    r = c.get("/")
    assert b"Test code" in r.data and b"Student ID" in r.data
    r = c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=UA)
    assert r.status_code == 302 and r.headers["Location"].endswith("/verify")
    r = c.get("/verify", headers=UA)
    assert b"Are you Lucas S.?" in r.data and b"Silva" not in r.data  # only first name and initial
    r = c.post("/verify", data={"answer": "yes"}, headers=UA, follow_redirects=True)
    assert b"Begin the test" in r.data and b"Lucas Silva" in r.data
    attempt_id = begin(app, c)
    r = c.get("/attempt", headers=UA)
    assert b'id="timer"' in r.data and b'class="watermark"' in r.data and b"MA2026008" in r.data
    assert b"data-block-copy" in r.data and b"/attempt/save" in r.data
    form = answers(app, attempt_id)
    r = c.post("/attempt/save", data=form, headers=UA)
    assert r.json["ok"] is True and r.json["remaining"] > 0 and r.json["locked"] is False
    r = c.post("/attempt/submit", data=form, headers=UA, follow_redirects=True)
    assert b"9 of 13 points" in r.data
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.status == "submitted" and attempt.score == 9.0
        assert [e.kind for e in attempt.events] == ["started", "submitted"]
    assert c.post("/attempt/save", data=form, headers=UA).status_code == 409
    # signing out ends the portal session
    c.post("/leave", headers=UA)
    assert c.get("/start", headers=UA).status_code == 302


def test_wrong_code_or_id_reveals_nothing(app):
    aid, code = demo(app)
    c = app.test_client()
    bad_code = c.post("/", data={"code": "ZZZZ-ZZZZ", "student_id": "MA2026008"})
    bad_id = c.post("/", data={"code": code, "student_id": "MA2026999"})
    other_course = c.post("/", data={"code": code, "student_id": ""})
    assert bad_code.status_code == bad_id.status_code == 400 and other_course.status_code == 400
    assert b"find a test with that code" in bad_code.data and b"find a test with that code" in bad_id.data
    # draft tests can't be entered
    demo(app, status="draft")
    assert c.post("/", data={"code": code, "student_id": "MA2026008"}).status_code == 400
    # codes are forgiving about case, spaces and dashes
    demo(app, status="published")
    loose = code[:4].lower() + " - " + code[4:].lower()
    assert c.post("/", data={"code": loose, "student_id": " ma2026008 "}).status_code == 302


def test_not_me_ends_the_session(app):
    aid, code = demo(app)
    c = app.test_client()
    c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=UA)
    r = c.post("/verify", data={"answer": "no"}, headers=UA)
    assert r.headers["Location"].endswith("/")
    assert c.get("/verify", headers=UA).status_code == 302
    with app.app_context():
        assert TestSession.query.one().status == "ended"


def test_students_cannot_use_the_staff_site(app):
    c = app.test_client()
    # students have no password at all
    with app.app_context():
        student = User.query.filter_by(username="demo.student1").one()
        assert student.password_hash is None and not student.can_sign_in
    assert b"Invalid username or password" in login(c, "demo.student1", "student123").data
    # a staff session whose user became a student is closed on the next request
    login(c, "demo.instructor", "instructor123")
    assert c.get("/staff/").status_code == 200
    with app.app_context():
        User.query.filter_by(username="demo.instructor").one().role = "student"
        db.session.commit()
    r = c.get("/staff/courses/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
    assert c.get("/staff/").status_code == 302


def test_attempt_limit_and_late_start(app):
    aid, code = demo(app)
    c = app.test_client()
    sign_in(c, code, "MA2026001")  # Amara already used 1 of 2 attempts in the demo
    attempt_id = begin(app, c)
    c.post("/attempt/submit", data={}, headers=UA)
    r = c.get("/start", headers=UA)
    assert b"used all of your attempts" in r.data
    assert b"Attempt 1" in r.data and b"Attempt 2" in r.data  # results stay reachable
    r = c.get(f"/results/{attempt_id}", headers=UA)
    assert r.status_code == 200
    # someone else's attempt is not reachable
    with app.app_context():
        other = Attempt.query.filter(Attempt.user.has(username="demo.student2")).first().id
    assert c.get(f"/results/{other}", headers=UA).status_code == 404
    # late start: opened an hour ago, no new starts after 10 minutes
    demo(app, opens_at=utcnow() - timedelta(hours=1), late_start_minutes=10)
    c2 = app.test_client()
    sign_in(c2, code)
    assert b"too late to start" in c2.get("/start", headers=UA).data
    assert c2.post("/start", headers=UA).headers["Location"].endswith("/start")


# --------------------------------------------------------------------------- identity


def test_pin_identity_check_and_pin_slips(app):
    aid, code = demo(app, identity_check="pin")
    c = app.test_client()
    c.post("/", data={"code": code, "student_id": "MA2026008"}, headers=UA)
    assert b"Your PIN" in c.get("/verify", headers=UA).data
    r = c.post("/verify", data={"answer": "yes"}, headers=UA)
    assert r.status_code == 403 and b"have a PIN yet" in r.data
    # the teacher prints PIN slips from the roster
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    with app.app_context():
        cid = db.session.get(Assessment, aid).course_id
        student_id = User.query.filter_by(username="demo.student8").one().id
    r = staff.post(f"/staff/courses/{cid}/pins", data={"scope": "one", "user_id": student_id})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    pin = re.search(rb'class="pin">(\d{6})<', r.data).group(1).decode()
    with app.app_context():
        stored = db.session.get(User, student_id).pin_hash
        assert stored and pin not in stored
    r = c.post("/verify", data={"answer": "yes", "pin": "000000" if pin != "000000" else "111111"}, headers=UA)
    assert r.status_code == 401 and b"not right" in r.data
    r = c.post("/verify", data={"answer": "yes", "pin": pin}, headers=UA)
    assert r.headers["Location"].endswith("/start")
    # five wrong PINs within 15 minutes lock that student out of this test, from any browser
    c3 = app.test_client()
    c3.post("/", data={"code": code, "student_id": "MA2026008"}, headers=UA)
    for _ in range(5):
        c3.post("/verify", data={"answer": "yes", "pin": "999999" if pin != "999999" else "888888"}, headers=UA)
    r = c3.post("/verify", data={"answer": "yes", "pin": pin}, headers=UA)
    assert r.status_code == 429
    # missing-PIN bulk generation only touches students without one
    r = staff.post(f"/staff/courses/{cid}/pins", data={"scope": "missing"})
    assert len(re.findall(rb'class="pin">\d{6}<', r.data)) == 7


def test_lookup_throttling(app):
    aid, code = demo(app)
    c = app.test_client()
    for _ in range(60):
        c.post("/", data={"code": "WRONGCODE", "student_id": "x"})
    r = c.post("/", data={"code": code, "student_id": "MA2026008"})
    assert r.status_code == 429 and b"Too many attempts" in r.data


def test_network_allowlist(app):
    aid, code = demo(app, allowed_networks="10.0.0.0/8")
    c = app.test_client()
    r = c.post("/", data={"code": code, "student_id": "MA2026008"})
    assert r.status_code == 403 and b"taken from the school" in r.data
    demo(app, allowed_networks="10.0.0.0/8\n127.0.0.0/8")
    assert c.post("/", data={"code": code, "student_id": "MA2026008"}).status_code == 302


# --------------------------------------------------------------------------- proctoring


def test_proctor_approval(app):
    aid, code = demo(app, require_approval=True)
    c = app.test_client()
    sign_in(c, code)
    r = c.get("/start", headers=UA)
    assert b"Waiting for your proctor" in r.data and b'http-equiv="refresh"' in r.data
    assert c.post("/start", headers=UA).status_code == 200  # can't start while waiting
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    r = staff.get(f"/staff/assessments/{aid}/monitor")
    assert b"Waiting to be let in" in r.data and b"Let in all 1 waiting" in r.data
    staff.post(f"/staff/assessments/{aid}/monitor/admit-all")
    assert b"Begin" in c.get("/start", headers=UA).data


def test_one_device_per_attempt_and_proctor_reset(app):
    aid, code = demo(app)
    laptop, phone = app.test_client(), app.test_client()
    sign_in(laptop, code)
    attempt_id = begin(app, laptop)
    r = sign_in(phone, code, headers={"User-Agent": "Phone"})
    assert r.status_code == 409 and b"already open on another device" in r.data
    assert laptop.post("/attempt/save", data={}, headers=UA).json["ok"] is True
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    with app.app_context():
        uid = db.session.get(Attempt, attempt_id).user_id
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/reset_device")
    assert laptop.post("/attempt/save", data={}, headers=UA).status_code == 401
    assert sign_in(phone, code, headers={"User-Agent": "Phone"}).headers["Location"].endswith("/start")
    phone.post("/start", headers={"User-Agent": "Phone"})
    assert phone.post("/attempt/save", data={}, headers={"User-Agent": "Phone"}).json["ok"] is True
    with app.app_context():
        kinds = [e.kind for e in db.session.get(Attempt, attempt_id).events]
        assert "device_blocked" in kinds and "device_reset" in kinds and "resumed" in kinds
    # without the device lock, a new sign-in takes the test over and the old browser is signed out
    demo(app, lock_to_device=False)
    tablet = app.test_client()
    sign_in(tablet, code, headers={"User-Agent": "Tablet"})
    assert phone.post("/attempt/save", data={}, headers={"User-Agent": "Phone"}).status_code == 401
    with app.app_context():
        assert "device_change" in [e.kind for e in db.session.get(Attempt, attempt_id).events]


def test_stolen_cookie_is_refused(app):
    aid, code = demo(app)
    c = app.test_client()
    sign_in(c, code)
    attempt_id = begin(app, c)
    token = c.get_cookie(PORTAL_COOKIE, path="/").value
    thief = app.test_client()
    thief.set_cookie(PORTAL_COOKIE, token, path="/")
    r = thief.post("/attempt/save", data={}, headers={"User-Agent": "Other browser"})
    assert r.status_code == 401
    # the session is now dead for everyone, and the attempt records why
    assert c.post("/attempt/save", data={}, headers=UA).status_code == 401
    with app.app_context():
        assert "session_hijack_blocked" in [e.kind for e in db.session.get(Attempt, attempt_id).events]


def test_leaving_the_page_pauses_after_the_limit_and_proctor_controls(app):
    aid, code = demo(app, max_focus_losses=2, require_fullscreen=True, time_limit_minutes=20)
    c = app.test_client()
    sign_in(c, code)
    attempt_id = begin(app, c)
    assert b"data-fullscreen" in c.get("/attempt", headers=UA).data
    r = c.post("/attempt/event", data={"kind": "focus_lost"}, headers=UA)
    assert r.json["locked"] is False
    # blur and visibilitychange arrive together: counted once
    c.post("/attempt/event", data={"kind": "focus_lost"}, headers=UA)
    with app.app_context():
        assert db.session.get(Attempt, attempt_id).focus_losses == 1
        for e in AttemptEvent.query.filter_by(attempt_id=attempt_id):
            e.created_at -= timedelta(seconds=10)
        db.session.commit()
    r = c.post("/attempt/event", data={"kind": "fullscreen_exit"}, headers=UA)
    assert r.json["locked"] is True and "2 times" in r.json["lock_reason"]
    assert c.post("/attempt/save", data={}, headers=UA).status_code == 423
    assert b"Your test is paused" in c.get("/attempt", headers=UA).data
    assert c.post("/attempt/event", data={"kind": "rm -rf"}, headers=UA).status_code == 400
    # the proctor lets the student continue, adds time, then submits for them
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    with app.app_context():
        uid = db.session.get(Attempt, attempt_id).user_id
    assert b"Paused" in staff.get(f"/staff/assessments/{aid}/monitor?fragment=1").data
    before = c.get("/attempt/status", headers=UA).json["remaining"]
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/unlock")
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/add_time", data={"minutes": "15"})
    assert staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/add_time", data={"minutes": "9999"}).status_code == 302
    status = c.get("/attempt/status", headers=UA).json
    assert status["locked"] is False and status["remaining"] >= before + 15 * 60 - 5
    staff.post(f"/staff/assessments/{aid}/monitor/student/{uid}/submit")
    r = c.get("/attempt/status", headers=UA)
    assert r.status_code == 409 and "/results/" in r.json["redirect"]
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        assert attempt.status == "submitted" and attempt.extra_minutes == 15 and attempt.focus_losses == 2
        kinds = [e.kind for e in attempt.events]
        for kind in ("locked", "unlocked", "time_added", "force_submitted"):
            assert kind in kinds
    # the timeline shows on the staff attempt page
    assert b"Integrity timeline" in staff.get(f"/staff/assessments/{aid}/attempts/{attempt_id}").data


def test_code_change_and_settings_validation(app):
    aid, code = demo(app)
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    staff.post(f"/staff/assessments/{aid}/monitor/code")
    new_code = demo(app)[1]
    assert new_code != code and len(new_code) == 8
    c = app.test_client()
    assert c.post("/", data={"code": code, "student_id": "MA2026008"}).status_code == 400
    form = {
        "title": "Unit 1 Check-in", "max_attempts": "2", "show_results": "immediately", "allow_online": "1",
        "identity_check": "pin", "require_approval": "1", "require_fullscreen": "1", "max_focus_losses": "3",
        "allowed_networks": "192.0.2.0/24\n2001:db8::/32", "access_code": "room-42b", "lock_to_device": "1",
    }
    r = staff.post(f"/staff/assessments/{aid}/settings", data=form, follow_redirects=True)
    assert b"Settings saved" in r.data
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert a.access_code == "ROOM42B" and a.identity_check == "pin" and a.require_approval and a.max_focus_losses == 3
        assert a.network_list == ["192.0.2.0/24", "2001:db8::/32"] and not a.block_copy_paste and not a.watermark
    bad = dict(form, allowed_networks="school wifi", max_focus_losses="0", late_start_minutes="5", access_code="abc")
    r = staff.post(f"/staff/assessments/{aid}/settings", data=bad)
    for message in (b"not an IP address", b"from 1 to 50", b"set when the test opens", b"at least 6"):
        assert message in r.data
    # copies get their own code
    staff.post(f"/staff/assessments/{aid}/duplicate", data={})
    with app.app_context():
        codes = [a.access_code for a in Assessment.query.all()]
        assert len(codes) == len(set(codes)) == 2


def test_roster_creates_students_without_accounts(app):
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    with app.app_context():
        cid = Assessment.query.filter_by(title="Unit 1 Check-in").one().course_id
    r = staff.post(f"/staff/courses/{cid}/roster", data={"action": "new_student", "full_name": "Zoe New", "student_number": "MA2026100"}, follow_redirects=True)
    assert b"Zoe New (MA2026100) is enrolled" in r.data
    csv_text = "student_id,name\nMA2026101,Ana Csv\nMA2026001,\n"
    r = staff.post(f"/staff/courses/{cid}/roster", data={"action": "import", "csv_text": csv_text}, follow_redirects=True)
    assert b"1 student(s) enrolled from CSV, 1 of them new" in r.data
    with app.app_context():
        zoe = User.query.filter_by(student_number="MA2026100").one()
        assert zoe.role == "student" and zoe.password_hash is None and zoe.auth_source == "portal"
    aid, code = demo(app)
    assert sign_in(app.test_client(), code, "MA2026100").headers["Location"].endswith("/start")


# --------------------------------------------------------------------------- hardening


def test_portal_security_headers_and_cookie(app):
    aid, code = demo(app)
    c = app.test_client()
    r = c.get("/")
    assert "script-src 'self'" in r.headers["Content-Security-Policy"] and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY" and "no-store" in r.headers["Cache-Control"]
    assert r.headers["Referrer-Policy"] == "same-origin" and "camera=()" in r.headers["Permissions-Policy"]
    r = c.post("/", data={"code": code, "student_id": "MA2026008"})
    cookie = r.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/;" in cookie
    with app.app_context():
        session = TestSession.query.one()
        assert session.token_hash not in cookie  # only a hash is stored
    # the identity confirmation rotates the token
    c.post("/verify", data={"answer": "yes"})
    with app.app_context():
        assert TestSession.query.one().token_hash != session.token_hash


def test_portal_requires_csrf_tokens(app):
    app.config["WTF_CSRF_ENABLED"] = True
    aid, code = demo(app)
    c = app.test_client()
    assert c.post("/", data={"code": code, "student_id": "MA2026008"}).status_code == 400
    html = c.get("/").data.decode()
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    assert c.post("/", data={"code": code, "student_id": "MA2026008", "csrf_token": token}).status_code == 302


def test_upgrade_moves_students_to_the_portal(app):
    with app.app_context():
        student = User.query.filter_by(username="demo.student3").one()
        student.set_password("old-password")
        student.auth_source = "local"
        a = Assessment.query.filter_by(title="Unit 1 Check-in").one()
        a.access_code = "room"
        db.session.get(Setting, "schema_version").value = "3"
        db.session.add(Setting(key="ldap_default_role", value="student"))
        db.session.commit()
        upgrade_schema()
        student = User.query.filter_by(username="demo.student3").one()
        assert student.password_hash is None and student.auth_source == "portal"
        assert len(Assessment.query.filter_by(title="Unit 1 Check-in").one().access_code) == 8
        assert db.session.get(Setting, "ldap_autoprovision").value == "0"
        assert db.session.get(Setting, "schema_version").value == str(SCHEMA_VERSION)


# --------------------------------------------------------------------------- analytics


def test_analytics_and_answer_similarity(app):
    aid, code = demo(app)
    with app.app_context():
        a = db.session.get(Assessment, aid)
        data = assessment_insights(a)
        assert data["n"] == 7 and data["scores"]["median"] is not None and data["split_half"] is not None
        assert sum(data["scores"]["levels"].values()) == 7
        assert data["item_points"] and all("tip" in p for p in data["item_points"])
        # two students who copy each other's wrong answers stand out
        attempts = list(a.attempts)
        choice_items = [i for i in a.items if i.question.qtype == "mc"]
        for attempt in attempts[-2:]:
            rmap = attempt.response_map()
            for item in choice_items:
                wrong = next(c for c in item.question.choices if not c.is_correct)
                rmap[item.id].set_answer(wrong.id)
                rmap[item.id].is_correct = False
        result = answer_similarity(attempts, list(a.items))
        assert result["checked"] and result["pairs"]
        top = result["pairs"][0]
        assert {top["a"].id, top["b"].id} == {attempts[-1].id, attempts[-2].id} and top["same"] >= 3
        db.session.rollback()
    staff = app.test_client()
    login(staff, "demo.instructor", "instructor123")
    r = staff.get(f"/staff/assessments/{aid}/insights")
    for text in (b"Score distribution", b"Reliability", b"Difficulty and discrimination", b"Standards on this test", b"Integrity", b"<svg"):
        assert text in r.data, text
    r = staff.get(f"/staff/assessments/{aid}/insights?format=csv")
    assert r.status_code == 200 and b"percentile_rank" in r.data and r.data.count(b"\n") == 8


# --------------------------------------------------------------------------- addresses and HTTPS


def test_portal_forms_work_over_https_with_a_browser_referer(app):
    """Behind nginx the app sees HTTPS, where form posts need a Referer: a no-referrer policy broke every sign-in."""
    app.config["WTF_CSRF_ENABLED"] = True
    aid, code = demo(app)
    c = app.test_client()
    https = {"base_url": "https://quiz.example.org"}
    r = c.get("/", **https)
    assert r.headers["Referrer-Policy"] == "same-origin"  # what the browser applies to this page's form
    token = re.search(r'name="csrf_token" value="([^"]+)"', r.data.decode()).group(1)
    form = {"code": code, "student_id": "MA2026008", "csrf_token": token}
    # what Safari then sends: a same-origin Referer
    r = c.post("/", data=form, headers={"Referer": "https://quiz.example.org/"}, **https)
    assert r.status_code == 302 and r.headers["Location"].endswith("/verify")
    # without it the request is refused, and the error page leads back to the portal, not to the staff site
    other = app.test_client()
    r = other.post("/", data=form, **https)
    assert r.status_code == 400 and b"Back to the start" in r.data and b"dashboard" not in r.data
    for page in ("/start", "/attempt"):
        assert c.get(page, **https).headers["Referrer-Policy"] == "same-origin"


def test_the_site_root_is_the_portal_and_staff_live_under_staff(app):
    c = app.test_client()
    assert b"Take a test" in c.get("/").data
    assert b"Sign in to Scholar" in c.get("/staff/login").data
    assert c.get("/staff/").headers["Location"].endswith("/staff/login?next=%2Fstaff%2F")
    for page in ("/login", "/courses/", "/staff/courses/") :
        assert c.get(page).status_code in (200, 302)
    # the old front door and the API keep working
    assert c.get("/api/v1/ping").status_code == 401


def test_old_addresses_redirect_to_the_new_ones(app):
    c = app.test_client()
    expected = {
        "/login": "/staff/login",
        "/courses/3?tab=roster": "/staff/courses/3?tab=roster",
        "/admin": "/staff/admin/",
        "/assessments/1/monitor": "/staff/assessments/1/monitor",
        "/test/": "/",
        "/test": "/",
        "/test/verify": "/verify",
        "/test/seb/ABC123.seb": "/seb/ABC123.seb",
    }
    for old, new in expected.items():
        r = c.get(old)
        assert r.status_code == 302, old
        assert r.headers["Location"].replace("http://localhost", "") == new, (old, r.headers["Location"])
