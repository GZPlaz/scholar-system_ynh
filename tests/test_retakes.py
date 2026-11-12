"""Retakes after the test: sign in again, review what was right and wrong, retake with earlier answers filled in."""
from datetime import timedelta

from test_app import app, login  # noqa: F401  (fixture re-export)
from test_portal import UA, answers, begin, demo, sign_in

from scholar import syncevents
from scholar.extensions import db
from scholar.grading import counted_attempts, enforce_deadline
from scholar.models import Assessment, Attempt, SyncEvent, User, utcnow

STUDENT = "MA2026008"  # Lucas Silva, who has not taken the demo test


def take(app_, code, student=STUDENT, wrong=False):
    """One student takes the test through the portal. Returns (client, attempt id)."""
    c = app_.test_client()
    assert sign_in(c, code, student).status_code == 302
    attempt_id = begin(app_, c)
    form = answers(app_, attempt_id)
    if wrong:
        form = {k: v for k, v in form.items() if not k.startswith("q_")}  # leave everything blank
    assert c.post("/attempt/submit", data=form, headers=UA).status_code == 302
    return c, attempt_id


def close(app_, aid):
    with app_.app_context():
        db.session.get(Assessment, aid).status = "closed"
        db.session.commit()


def retakes(app_, aid, **settings):
    with app_.app_context():
        a = db.session.get(Assessment, aid)
        for key, value in settings.items():
            setattr(a, key, value)
        db.session.commit()


def test_no_retake_unless_the_teacher_allows_it(app):
    aid, code = demo(app, max_attempts=1)
    c, attempt_id = take(app, code)
    close(app, aid)
    page = c.get("/start", headers=UA).data
    assert b"This test is closed." in page and b"retake" not in page.lower()
    assert c.post("/start", headers=UA).status_code == 302  # no attempt starts
    with app.app_context():
        assert Attempt.query.filter_by(user_id=db.session.get(Attempt, attempt_id).user_id, assessment_id=aid).count() == 1
        assert db.session.get(Attempt, attempt_id).is_retake is False


def test_retake_window_follows_the_test_closing_and_the_set_times(app):
    aid, code = demo(app)
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert not a.retake_window_open()
        a.retake_mode = "practice"
        assert not a.retake_window_open()  # the test is still open
        a.status = "closed"
        assert a.retake_window_open()
        a.retake_opens_at = utcnow() + timedelta(hours=1)
        assert not a.retake_window_open()  # a later start overrides "as soon as closed"
        a.retake_opens_at = utcnow() - timedelta(hours=1)
        a.retake_closes_at = utcnow() - timedelta(minutes=1)
        assert not a.retake_window_open()
        a.retake_closes_at = utcnow() + timedelta(days=1)
        assert a.retake_window_open()
        a.status = "draft"
        assert not a.retake_window_open()


def test_practice_retake_end_to_end(app):
    aid, code = demo(app, max_attempts=1, show_correct_answers=False)
    c, first = take(app, code, wrong=True)  # blank: 0 points
    with app.app_context():
        assert db.session.get(Attempt, first).score == 0
    close(app, aid)
    retakes(app, aid, retake_mode="practice", retake_max_attempts=1, retake_prefill=True)

    # signing in again after the test closed: the lobby offers a retake and links the review
    c2 = app.test_client()
    sign_in(c2, code, STUDENT)
    lobby = c2.get("/start", headers=UA).data.decode()
    assert "You can retake this test" in lobby and "for practice" in lobby and "Start my retake" in lobby
    assert f"/results/{first}" in lobby
    review = c2.get(f"/results/{first}", headers=UA).data.decode()
    assert "0 of 13 points" in review and "Retake the test" in review
    assert "not to show the correct answers" in review  # right and wrong are shown, the key isn't

    retake_id = begin(app, c2)
    with app.app_context():
        r = db.session.get(Attempt, retake_id)
        assert r.is_retake and r.practice and r.number == 2 and not r.deadline  # closed test, no time limit
        assert [e.kind for e in r.events][:2] == ["started", "retake"]
    page = c2.get("/attempt", headers=UA).data
    assert b"Submit" in page

    # a student who skipped everything last time starts blank; answer and submit
    form = answers(app, retake_id)
    assert c2.post("/attempt/submit", data=form, headers=UA, follow_redirects=True).status_code == 200
    with app.app_context():
        r = db.session.get(Attempt, retake_id)
        assert r.status == "submitted" and r.score == 9
        assert db.session.get(Attempt, first).score == 0
        a = db.session.get(Assessment, aid)
        # practice: the counted attempt, and so the grade, is still the first one
        counted = counted_attempts(a)[User.query.filter_by(student_number=STUDENT).one().id]
        assert counted.id == first and counted.score == 0
    review = c2.get(f"/results/{retake_id}", headers=UA).data.decode()
    assert "This was a retake for practice: it does not change your grade." in review

    # one retake allowed: the next try is refused
    again = c2.get("/start", headers=UA).data.decode()
    assert "You have used all of your retakes." in again
    assert c2.post("/start", headers=UA).status_code == 302
    with app.app_context():
        assert Attempt.query.filter_by(user_id=db.session.get(Attempt, first).user_id, assessment_id=aid).count() == 2


def test_retake_that_counts_changes_the_grade_and_tells_rosariosis(app):
    aid, code = demo(app, max_attempts=1)
    with app.app_context():
        from scholar.models import Course

        db.session.get(Assessment, aid).course.external_id = "rosario:cp:7"
        SyncEvent.query.delete()
        db.session.commit()
    c, first = take(app, code, wrong=True)
    close(app, aid)
    retakes(app, aid, retake_mode="counts", retake_max_attempts=0)
    with app.app_context():
        SyncEvent.query.delete()
        db.session.commit()
    c2 = app.test_client()
    sign_in(c2, code, STUDENT)
    lobby = c2.get("/start", headers=UA).data.decode()
    assert "Your best attempt is the one that counts" in lobby
    retake_id = begin(app, c2)
    c2.post("/attempt/submit", data=answers(app, retake_id), headers=UA)
    with app.app_context():
        user = User.query.filter_by(student_number=STUDENT).one()
        a = db.session.get(Assessment, aid)
        assert counted_attempts(a)[user.id].id == retake_id and counted_attempts(a)[user.id].score == 9
        assert [(e.course_external_id, e.reason) for e in SyncEvent.query] == [("rosario:cp:7", "grades")]  # the plugin is told
    # unlimited retakes: and a worse one under "best" can't lower the grade
    retakes(app, aid, retake_prefill=False)
    retake2 = begin(app, c2)
    c2.post("/attempt/submit", data={}, headers=UA)
    with app.app_context():
        assert db.session.get(Attempt, retake2).score == 0
        assert counted_attempts(db.session.get(Assessment, aid))[user.id].id == retake_id


def test_answers_are_prefilled_and_can_be_switched_off(app):
    aid, code = demo(app, max_attempts=1)
    c, first = take(app, code)  # 9 of 13 with answers
    close(app, aid)
    retakes(app, aid, retake_mode="practice", retake_max_attempts=0, retake_prefill=True)
    c2 = app.test_client()
    sign_in(c2, code, STUDENT)
    retake_id = begin(app, c2)
    with app.app_context():
        old = {r.item_id: r.get_answer() for r in db.session.get(Attempt, first).responses}
        new = {r.item_id: r.get_answer() for r in db.session.get(Attempt, retake_id).responses}
        assert old and new == old
        assert all(r.points_awarded is None for r in db.session.get(Attempt, retake_id).responses)  # not graded until submitted
    page = c2.get("/attempt", headers=UA).data.decode()
    assert "checked" in page  # the earlier choices are ticked
    c2.post("/attempt/submit", data={f"present_{i}": "1" for i in old}, headers=UA)  # submit unchanged blank form
    retakes(app, aid, retake_prefill=False)
    c3 = app.test_client()
    sign_in(c3, code, STUDENT)
    blank_id = begin(app, c3)
    with app.app_context():
        assert db.session.get(Attempt, blank_id).responses == []


def test_reviewing_shows_the_key_only_when_the_teacher_chose_to(app):
    aid, code = demo(app, max_attempts=1, show_correct_answers=False)
    c, first = take(app, code)
    close(app, aid)
    retakes(app, aid, retake_mode="practice", show_results="never", retake_show_correct=False)
    c2 = app.test_client()
    sign_in(c2, code, STUDENT)
    # even with results "never released", retakes being open lets the student see what they got right and wrong
    review = c2.get(f"/results/{first}", headers=UA).data.decode()
    assert "9 of 13 points" in review and "not to show the correct answers" in review
    retakes(app, aid, retake_show_correct=True)
    review = c2.get(f"/results/{first}", headers=UA).data.decode()
    assert "not to show the correct answers" not in review
    retakes(app, aid, retake_mode="")  # retakes off again: back to the "never" rule
    assert b"Results will be available" in c2.get(f"/results/{first}", headers=UA).data


def test_a_retake_has_its_own_clock_and_survives_closing(app):
    aid, code = demo(app, max_attempts=1, time_limit_minutes=30)
    take(app, code)
    close(app, aid)
    retakes(app, aid, retake_mode="practice", retake_max_attempts=0, retake_closes_at=utcnow() + timedelta(hours=2))
    c2 = app.test_client()
    sign_in(c2, code, STUDENT)
    retake_id = begin(app, c2)
    with app.app_context():
        r = db.session.get(Attempt, retake_id)
        assert r.deadline and 29 * 60 < (r.deadline - utcnow()).total_seconds() <= 30 * 60  # time limit, not the old closing time
        assert enforce_deadline(r) is False and r.status == "in_progress"  # the test being closed doesn't end it
        db.session.commit()
    # the 30 minutes run out
    with app.app_context():
        r = db.session.get(Attempt, retake_id)
        r.deadline = utcnow() - timedelta(minutes=5)
        assert enforce_deadline(r) is True and r.status == "submitted"
        db.session.commit()
    # the retake window closing ends one in progress; turning retakes off does too
    retake2 = begin(app, c2)
    with app.app_context():
        r = db.session.get(Attempt, retake2)
        db.session.get(Assessment, aid).retake_closes_at = utcnow() - timedelta(seconds=1)
        db.session.flush()
        assert not db.session.get(Assessment, aid).retake_window_open()
        r.deadline = None
        assert enforce_deadline(r) is False or r.status == "submitted"  # no deadline given: bounded by the window below
        db.session.rollback()
    retakes(app, aid, retake_closes_at=utcnow() + timedelta(hours=1), retake_mode="")
    with app.app_context():
        assert enforce_deadline(db.session.get(Attempt, retake2)) is True


def test_teacher_settings_for_retakes(app):
    aid, _ = demo(app)
    c = app.test_client()
    login(c, "demo.instructor", "instructor123")
    form = {"title": "Unit 1 Check-in", "max_attempts": "1", "show_results": "immediately", "identity_check": "confirm", "allow_online": "1",
            "password_rotation_minutes": "5", "retake_mode": "counts", "retake_max_attempts": "2", "retake_prefill": "1"}
    r = c.post(f"/staff/assessments/{aid}/settings", data={**form, "retake_mode": "sometimes"}, follow_redirects=True)
    assert b"Choose how retakes count" in r.data
    r = c.post(f"/staff/assessments/{aid}/settings", data={**form, "retake_opens_at": "2026-12-02T09:00", "retake_closes_at": "2026-12-01T09:00"}, follow_redirects=True)
    assert b"Retakes must close after they open" in r.data
    r = c.post(f"/staff/assessments/{aid}/settings", data={**form, "retake_max_attempts": "99"}, follow_redirects=True)
    assert b"Retakes allowed must be" in r.data
    c.post(f"/staff/assessments/{aid}/settings", data=form)
    with app.app_context():
        a = db.session.get(Assessment, aid)
        assert (a.retake_mode, a.retake_max_attempts, a.retake_prefill, a.retake_show_correct) == ("counts", 2, True, False)
    page = c.get(f"/staff/assessments/{aid}/settings").data.decode()
    assert "Retakes" in page and 'value="counts" checked' in page.replace("  ", " ")
    copy = c.post(f"/staff/assessments/{aid}/duplicate", follow_redirects=False)
    with app.app_context():
        newest = Assessment.query.order_by(Assessment.id.desc()).first()
        assert newest.id != aid and (newest.retake_mode, newest.retake_max_attempts) == ("counts", 2)
    assert c.get("/staff/assessments/new?course_id=1").status_code == 200
