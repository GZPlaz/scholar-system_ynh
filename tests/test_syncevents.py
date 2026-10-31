"""Grade changes reach connected systems by themselves: the change log, the change feed and the signed webhook."""
import json
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from test_api import auth, make_token
from test_app import app, login  # noqa: F401  (fixture re-export)

from scholar import syncevents
from scholar.extensions import db
from scholar.grading import finalize_attempt, recompute_attempt, regrade_assessment, start_attempt
from scholar.models import Assessment, Attempt, Course, Question, Response, Setting, SyncEvent, User, utcnow


@pytest.fixture()
def linked(app):
    """The demo course, linked to a RosarioSIS course period, with its demo history forgotten."""
    with app.app_context():
        course = Course.query.filter_by(code="DEMO101").one()
        course.external_id = "rosario:cp:7"
        SyncEvent.query.delete()
        db.session.commit()
        return {"course": course.id, "assessment": Assessment.query.filter_by(course_id=course.id).first().id}


def events():
    return [(e.course_external_id, e.assessment_id, e.reason) for e in SyncEvent.query.order_by(SyncEvent.id)]


def new_attempt(assessment_id, username="demo.student6", answer_all=True):
    assessment = db.session.get(Assessment, assessment_id)
    user = User.query.filter_by(username=username).one()
    attempt = start_attempt(assessment, user)
    db.session.commit()
    return attempt.id


def test_submitting_an_attempt_records_a_change(app, linked):
    with app.app_context():
        attempt_id = new_attempt(linked["assessment"])
        assert events() == []  # a started attempt isn't a grade yet
        finalize_attempt(db.session.get(Attempt, attempt_id))
        db.session.commit()
        assert events() == [("rosario:cp:7", linked["assessment"], "grades")]


def test_unlinked_courses_record_nothing(app, linked):
    with app.app_context():
        db.session.get(Course, linked["course"]).external_id = None
        db.session.commit()
        attempt_id = new_attempt(linked["assessment"])
        finalize_attempt(db.session.get(Attempt, attempt_id))
        db.session.commit()
        assert events() == []


def test_rolled_back_changes_leave_no_event(app, linked):
    with app.app_context():
        attempt_id = new_attempt(linked["assessment"])
        finalize_attempt(db.session.get(Attempt, attempt_id))
        db.session.flush()
        assert len(events()) == 1
        db.session.rollback()
        assert events() == []


def test_manual_grading_regrading_and_deleting_are_noticed(app, linked):
    with app.app_context():
        assessment = db.session.get(Assessment, linked["assessment"])
        pending = next(a for a in assessment.attempts if a.status == "submitted" and a.needs_grading)
        attempt_id = pending.id
    c = app.test_client()
    login(c, "demo.instructor", "instructor123")
    aid = linked["assessment"]
    with app.app_context():
        attempt = db.session.get(Attempt, attempt_id)
        form = {f"points_{r.item_id}": "2" for r in attempt.responses if r.needs_grading}
    assert form
    assert c.post(f"/staff/assessments/{aid}/attempts/{attempt_id}", data=form).status_code == 302
    with app.app_context():
        assert events() == [("rosario:cp:7", aid, "grades")]
        assert not db.session.get(Attempt, attempt_id).needs_grading
    # saving the same grades again changes nothing, so nothing is sent
    c.post(f"/staff/assessments/{aid}/attempts/{attempt_id}", data=form)
    with app.app_context():
        assert len(events()) == 1
    # a corrected answer key re-scores the test
    with app.app_context():
        choice_question = next(i.question for i in db.session.get(Assessment, aid).items if i.question.qtype == "mc")
        wrong, right = [ch for ch in choice_question.choices if not ch.is_correct][0], next(ch for ch in choice_question.choices if ch.is_correct)
        wrong.is_correct, right.is_correct = True, False
        regrade_assessment(db.session.get(Assessment, aid))
        db.session.commit()
        assert len(events()) == 2 and events()[-1][2] in ("grades", "questions changed")
    c.post(f"/staff/assessments/{aid}/attempts/{attempt_id}/delete")
    with app.app_context():
        assert events()[-1] == ("rosario:cp:7", aid, "attempt deleted")


def test_publishing_closing_and_the_attempt_policy_are_noticed(app, linked):
    with app.app_context():
        assessment = db.session.get(Assessment, linked["assessment"])
        assessment.status = "closed"
        db.session.commit()
        assert events() == [("rosario:cp:7", linked["assessment"], "test updated")]
        SyncEvent.query.delete()
        db.session.add(Setting(key="attempt_policy", value="best"))
        db.session.commit()
        assert events() == []  # a new setting row isn't a change of policy
        db.session.get(Setting, "attempt_policy").value = "latest"
        db.session.commit()
        assert events() == [("rosario:cp:7", None, "setting")]


def test_change_feed(app, linked):
    c = app.test_client()
    token = make_token(app)
    with app.app_context():
        finalize_attempt(db.session.get(Attempt, new_attempt(linked["assessment"])))
        db.session.commit()
    assert c.get("/api/v1/changes").status_code == 401
    data = c.get("/api/v1/changes", headers=auth(token)).json
    assert data["head"] >= 1 and data["more"] is False
    event = data["events"][-1]
    assert event["external_id"] == "rosario:cp:7" and event["course_id"] == linked["course"] and event["created_at"].endswith("Z")
    assert c.get(f"/api/v1/changes?after={data['head']}", headers=auth(token)).json["events"] == []
    first = c.get("/api/v1/changes?limit=1", headers=auth(token)).json
    assert len(first["events"]) == 1 and first["more"] is (data["head"] > first["events"][0]["id"])


# --------------------------------------------------------------------------- the webhook


class Receiver:
    """A tiny web server standing in for RosarioSIS."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append((dict(self.headers), body))
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/webhook.php"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def receiver():
    r = Receiver()
    yield r
    r.close()


def settle():
    """Let events be old enough to be sent (they wait a moment so bursts go out together)."""
    SyncEvent.query.update({SyncEvent.created_at: utcnow() - timedelta(seconds=10)})
    db.session.commit()


def test_registering_the_webhook_over_the_api(app, receiver):
    c = app.test_client()
    token = make_token(app)
    assert c.put("/api/v1/webhook", json={"url": "ftp://x"}, headers=auth(token)).status_code == 400
    assert c.put("/api/v1/webhook", json={"url": "https://u:p@x.org/h"}, headers=auth(token)).status_code == 400
    assert c.put("/api/v1/webhook", json={}, headers=auth(token)).status_code == 400
    r = c.put("/api/v1/webhook", json={"url": receiver.url}, headers=auth(token))
    assert r.status_code == 200 and len(r.json["secret"]) == 64 and r.json["webhook"]["url"] == receiver.url
    assert c.put("/api/v1/webhook", json={"url": receiver.url}, headers=auth(token)).json["secret"] == r.json["secret"]
    assert c.put("/api/v1/webhook", json={"url": receiver.url, "rotate": True}, headers=auth(token)).json["secret"] != r.json["secret"]
    secret = c.get("/api/v1/webhook", headers=auth(token)).json
    assert "secret" not in secret and secret["webhook"]["configured"]
    ok = c.post("/api/v1/webhook/test", headers=auth(token)).json
    assert ok["ok"] is True and len(receiver.requests) == 1
    assert json.loads(receiver.requests[0][1])["event"] == "ping"
    assert c.delete("/api/v1/webhook", headers=auth(token)).json["webhook"]["configured"] is False


def test_deliveries_are_signed_and_combined_per_course(app, linked, receiver):
    with app.app_context():
        secret = syncevents.save_webhook(receiver.url)
        db.session.commit()
        for name in ("demo.student6", "demo.student7"):
            finalize_attempt(db.session.get(Attempt, new_attempt(linked["assessment"], name)))
            db.session.commit()
        assert len(events()) == 2
        assert syncevents.deliver_due() is not None  # too fresh: waits for the burst to finish
        assert receiver.requests == []
        settle()
        assert syncevents.deliver_due() is None
    assert len(receiver.requests) == 1  # one request for both changes
    headers, body = receiver.requests[0]
    stamp, signature = headers["X-Scholar-Timestamp"], headers["X-Scholar-Signature"]
    assert signature == "sha256=" + syncevents.sign(secret, stamp, body) and abs(time.time() - int(stamp)) < 60
    payload = json.loads(body)
    assert payload["event"] == "grades.changed" and payload["course"] == {"id": linked["course"], "external_id": "rosario:cp:7"}
    assert payload["assessments"] == [linked["assessment"]] and payload["changes"] == 2
    with app.app_context():
        assert all(e.delivered_at for e in SyncEvent.query)
        assert syncevents.stats()["pending"] == 0
    # a forged signature (wrong secret) would not match
    assert signature != "sha256=" + syncevents.sign("not-the-secret", stamp, body)


def test_failed_deliveries_are_retried_then_succeed(app, linked):
    receiver = Receiver(status=500)
    try:
        with app.app_context():
            syncevents.save_webhook(receiver.url)
            finalize_attempt(db.session.get(Attempt, new_attempt(linked["assessment"])))
            db.session.commit()
            settle()
            wait = syncevents.deliver_due()
            row = SyncEvent.query.one()
            assert wait == syncevents.RETRY_PAUSES[0] and row.attempts == 1 and row.last_error == "HTTP 500" and row.delivered_at is None
            assert syncevents.stats()["pending"] == 1
            # not due yet: nothing is sent again
            before = len(receiver.requests)
            assert syncevents.deliver_due() > 0 and len(receiver.requests) == before
            # the receiver recovers and the retry time comes
            receiver.status = 200
            row.next_attempt_at = utcnow() - timedelta(seconds=1)
            db.session.commit()
            assert syncevents.deliver_due() is None
            row = SyncEvent.query.one()
            assert row.delivered_at is not None and row.attempts == 2 and row.last_error == ""
    finally:
        receiver.close()


def test_unreachable_receiver_gives_up_but_keeps_the_feed(app, linked):
    with app.app_context():
        syncevents.save_webhook("http://127.0.0.1:9/webhook.php")  # nothing listens there
        finalize_attempt(db.session.get(Attempt, new_attempt(linked["assessment"])))
        db.session.commit()
        settle()
        for _ in range(syncevents.MAX_ATTEMPTS):
            SyncEvent.query.update({SyncEvent.next_attempt_at: None})
            db.session.commit()
            syncevents.deliver_due()
        stats = syncevents.stats()
        assert stats["pending"] == 0 and stats["given_up"] == 1 and "connect" in stats["last_error"].lower()
        assert syncevents.deliver_due() is None
    token = make_token(app)
    assert app.test_client().get("/api/v1/changes", headers=auth(token)).json["events"]  # still readable by polling


def test_redirects_are_not_followed(app, linked):
    receiver = Receiver(status=302)
    try:
        ok, message = syncevents.post(receiver.url, "secret", {"event": "ping"})
        assert not ok and "302" in message
    finally:
        receiver.close()


def test_admin_page_manages_the_webhook(app, linked, receiver):
    c = app.test_client()
    login(c, "admin", "adminpass123")
    r = c.post("/staff/admin/integrations", data={"action": "webhook_save", "url": "nonsense"}, follow_redirects=True)
    assert b"must start with https" in r.data
    r = c.post("/staff/admin/integrations", data={"action": "webhook_save", "url": receiver.url}, follow_redirects=True)
    assert b"Saved." in r.data and receiver.url.encode() in r.data
    r = c.post("/staff/admin/integrations", data={"action": "webhook_test"}, follow_redirects=True)
    assert b"The other system answered the test" in r.data
    with app.app_context():
        finalize_attempt(db.session.get(Attempt, new_attempt(linked["assessment"])))
        db.session.commit()
    r = c.get("/staff/admin/integrations")
    assert b"rosario:cp:7" in r.data and b"1 waiting" in r.data
    r = c.post("/staff/admin/integrations", data={"action": "webhook_clear"}, follow_redirects=True)
    assert b"Instant updates are off" in r.data
    other = app.test_client()
    login(other, "demo.instructor", "instructor123")
    assert other.post("/staff/admin/integrations", data={"action": "webhook_save", "url": receiver.url}).status_code == 403
