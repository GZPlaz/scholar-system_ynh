"""Tell connected systems (the RosarioSIS plugin) the moment grades change.

Nothing here is called from the grading code. A hook on the database session watches what is
about to be saved: when a submitted attempt is added, rescored, regraded, finished (essay
graded by hand) or deleted, or when a test's status, title, dates or points change, a
``SyncEvent`` row is written in the *same transaction*, for the course the test belongs to (if
the course is linked to another system). That covers every present and future way of changing a
grade: online submission, deadline auto-submit, proctor force-submit, manual grading, regrades
after an answer key edit, paper answer sheets, scans, PDF key changes, deleting an attempt.

Delivery is separate. A background thread sends one signed request per course to the webhook
address (retrying with growing pauses); the plugin answers by copying the grades. The rows
also make up a change feed (``GET /api/v1/changes``) the plugin polls as a safety net, so a
missed delivery only delays a sync and never loses it.
"""
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from urllib.parse import urlparse

from flask import current_app, has_app_context
from sqlalchemy import event, inspect, or_, update
from sqlalchemy.orm import Session

from . import __version__
from .extensions import db
from .models import Assessment, AssessmentItem, Attempt, Course, Setting, SyncEvent, utcnow

log = logging.getLogger("scholar.syncevents")

DEBOUNCE = timedelta(seconds=2)  # a burst of changes (a regrade, a scan batch) goes out as one request
LOCK_TIME = timedelta(seconds=90)  # a crashed delivery is retried after this
RETRY_PAUSES = [10, 30, 120, 600, 1800, 3600, 3600, 7200]  # seconds; then the plugin's own polling takes over
MAX_ATTEMPTS = len(RETRY_PAUSES) + 1
KEEP_DAYS = 14
TIMEOUT = 25  # seconds the plugin has to copy the grades
SIGNATURE_TOLERANCE = 300  # seconds a receiver should accept a timestamp

_KEY = "scholar_sync_events"
_ATTEMPT_FIELDS = ("status", "score", "max_score", "needs_grading", "submitted_at")
_ASSESSMENT_FIELDS = ("status", "title", "opens_at", "closes_at")
_ITEM_FIELDS = ("points", "position")


# --------------------------------------------------------------------------- recording changes


def _changed(obj, names):
    state = inspect(obj)
    return any(state.attrs[name].history.has_changes() for name in names)


def _was(obj, name):
    """The value an attribute had before this change."""
    history = inspect(obj).attrs[name].history
    return history.deleted[0] if history.deleted else getattr(obj, name)


def _collect(session):
    """What is about to be saved that changes a grade or a test: {(course_id, assessment_id): reason}."""
    found = {}

    def add(course_id, assessment_id, reason):
        if course_id is not None:
            found.setdefault((course_id, assessment_id), reason)

    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        new, deleted = obj in session.new, obj in session.deleted
        if isinstance(obj, Attempt):
            assessment = obj.assessment
            if assessment is None or assessment.id is None:
                continue
            if new:
                counts = obj.status == "submitted"
            elif deleted:
                counts = _was(obj, "status") == "submitted"
            else:
                counts = (obj.status == "submitted" or _was(obj, "status") == "submitted") and _changed(obj, _ATTEMPT_FIELDS)
            if counts:
                add(assessment.course_id, assessment.id, "attempt deleted" if deleted else "grades")
        elif isinstance(obj, Assessment):
            if obj.id is None:
                continue
            if deleted:
                add(obj.course_id, obj.id, "test deleted")
            elif not new and _changed(obj, _ASSESSMENT_FIELDS):
                add(obj.course_id, obj.id, "test updated")
        elif isinstance(obj, AssessmentItem):
            assessment = obj.assessment
            if assessment is not None and assessment.id is not None and (new or deleted or _changed(obj, _ITEM_FIELDS)):
                add(assessment.course_id, assessment.id, "questions changed")
        elif isinstance(obj, Setting) and obj.key == "attempt_policy" and not new and _changed(obj, ("value",)):
            # Best vs. latest attempt changes the counted grade of every test.
            for (course_id,) in session.query(Course.id).filter(Course.external_id.isnot(None)):
                add(course_id, None, "setting")
    return found


@event.listens_for(Session, "before_flush")
def _before_flush(session, context, instances):
    with session.no_autoflush:
        found = _collect(session)
        if not found:
            return
        # One transaction can flush many times (regrading a class loads things as it goes):
        # record each (course, test) once.
        recorded = session.info.setdefault(_KEY, set())
        found = {key: reason for key, reason in found.items() if key not in recorded}
        if not found:
            return
        externals = dict(session.query(Course.id, Course.external_id).filter(Course.id.in_({course_id for course_id, _ in found})))
        for (course_id, assessment_id), reason in found.items():
            recorded.add((course_id, assessment_id))
            if externals.get(course_id):  # courses that aren't linked to another system need no event
                session.add(
                    SyncEvent(course_id=course_id, course_external_id=externals[course_id], assessment_id=assessment_id, reason=reason)
                )


@event.listens_for(Session, "after_commit")
def _after_commit(session):
    if session.info.pop(_KEY, None):
        kick()


@event.listens_for(Session, "after_rollback")
def _after_rollback(session):
    session.info.pop(_KEY, None)


# --------------------------------------------------------------------------- the webhook setting


def _setting(key, default=""):
    row = db.session.get(Setting, key)
    return row.value if row is not None else default


def _put_setting(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        db.session.add(Setting(key=key, value=value))
    else:
        row.value = value


def webhook():
    """The saved webhook as (url, secret); ("", "") when none is set."""
    url = _setting("webhook_url")
    return (url, _setting("webhook_secret")) if url else ("", "")


def clean_url(raw):
    """A usable webhook address, or raises ValueError with a message for the administrator."""
    url = (raw or "").strip()
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The address must start with https:// (or http://) and name a server.")
    if parts.username or parts.password:
        raise ValueError("Leave user names and passwords out of the address.")
    if len(url) > 500:
        raise ValueError("The address is too long.")
    return url


def save_webhook(url, rotate=False):
    """Save the address (keeping the signing secret unless asked) and return the secret."""
    url = clean_url(url)
    secret = _setting("webhook_secret")
    if not secret or rotate:
        secret = secrets.token_hex(32)
    _put_setting("webhook_url", url)
    _put_setting("webhook_secret", secret)
    return secret


def clear_webhook():
    _put_setting("webhook_url", "")
    _put_setting("webhook_secret", "")


def sign(secret, timestamp, body):
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(NoRedirects)


def post(url, secret, payload):
    """Send one signed request. Returns (ok, message). Never raises."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"Scholar/{__version__}",
            "X-Scholar-Event": payload.get("event", ""),
            "X-Scholar-Timestamp": stamp,
            "X-Scholar-Signature": f"sha256={sign(secret, stamp, body)}",
        },
    )
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:
            response.read(2048)
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        hint = " (the address redirects: use the final address)" if 300 <= exc.code < 400 else ""
        return False, f"HTTP {exc.code}{hint}"
    except urllib.error.URLError as exc:
        return False, f"Could not connect: {exc.reason}"[:280]
    except Exception as exc:  # timeouts, TLS problems, bad addresses
        return False, f"{type(exc).__name__}: {exc}"[:280]


def send_test():
    """Ask the receiver to answer a signed 'ping'. Returns (ok, message)."""
    url, secret = webhook()
    if not url:
        return False, "No webhook address is set."
    return post(url, secret, {"event": "ping", "sent_at": utcnow().isoformat() + "Z", "instance": _setting("institution_name")})


# --------------------------------------------------------------------------- delivery


def deliver_due():
    """Send what is due. Returns the seconds until the next retry, or None when nothing is waiting."""
    url, secret = webhook()
    now = utcnow()
    pending = (
        SyncEvent.query.filter(SyncEvent.delivered_at.is_(None), SyncEvent.attempts < MAX_ATTEMPTS)
        .order_by(SyncEvent.id)
        .all()
    )
    if not url or not pending:
        return None
    by_course = {}
    for row in pending:
        by_course.setdefault(row.course_id, []).append(row)
    wait = None
    for course_id, rows in by_course.items():
        due_at = max(
            max(r.created_at for r in rows) + DEBOUNCE,
            max((r.next_attempt_at for r in rows if r.next_attempt_at), default=now),
            max((r.locked_until for r in rows if r.locked_until), default=now),
        )
        if due_at > now:
            seconds = (due_at - now).total_seconds()
            wait = seconds if wait is None else min(wait, seconds)
            continue
        ids = [r.id for r in rows]
        claimed = db.session.execute(
            update(SyncEvent)
            .where(SyncEvent.id.in_(ids), SyncEvent.delivered_at.is_(None), or_(SyncEvent.locked_until.is_(None), SyncEvent.locked_until < now))
            .values(locked_until=now + LOCK_TIME)
        ).rowcount
        db.session.commit()
        if claimed != len(ids):  # another worker process took them
            continue
        payload = {
            "event": "grades.changed",
            "sent_at": now.isoformat() + "Z",
            "event_id": max(ids),
            "course": {"id": course_id, "external_id": rows[0].course_external_id},
            "assessments": sorted({r.assessment_id for r in rows if r.assessment_id}),
            "reasons": sorted({r.reason for r in rows if r.reason}),
            "changes": len(rows),
        }
        ok, message = post(url, secret, payload)
        finished = utcnow()
        pauses = []
        for row in SyncEvent.query.filter(SyncEvent.id.in_(ids)).all():
            row.locked_until = None
            row.attempts += 1
            if ok:
                row.delivered_at = finished
                row.next_attempt_at = None
                row.last_error = ""
            else:
                pause = RETRY_PAUSES[min(row.attempts - 1, len(RETRY_PAUSES) - 1)]
                pauses.append(pause)
                row.next_attempt_at = finished + timedelta(seconds=pause)
                row.last_error = message[:300]
        db.session.commit()
        if ok:
            log.info("Told %s about %d change(s) in course %s", urlparse(url).netloc, len(ids), course_id)
        else:
            log.warning("Could not tell %s about course %s: %s", urlparse(url).netloc, course_id, message)
            pause = min(pauses) if pauses else RETRY_PAUSES[0]
            wait = pause if wait is None else min(wait, pause)
    prune()
    return wait


def prune():
    """Drop old events now and then (the table gains a row per submitted attempt)."""
    if secrets.randbelow(40):
        return
    cutoff = utcnow() - timedelta(days=KEEP_DAYS)
    SyncEvent.query.filter(SyncEvent.created_at < cutoff).delete(synchronize_session=False)
    db.session.commit()


def stats():
    """Numbers for the Integrations page."""
    pending = SyncEvent.query.filter(SyncEvent.delivered_at.is_(None), SyncEvent.attempts < MAX_ATTEMPTS).count()
    given_up = SyncEvent.query.filter(SyncEvent.delivered_at.is_(None), SyncEvent.attempts >= MAX_ATTEMPTS).count()
    last_ok = db.session.query(db.func.max(SyncEvent.delivered_at)).scalar()
    failing = (
        SyncEvent.query.filter(SyncEvent.delivered_at.is_(None), SyncEvent.last_error != "").order_by(SyncEvent.id.desc()).first()
    )
    return {"pending": pending, "given_up": given_up, "last_ok": last_ok, "last_error": failing.last_error if failing else ""}


# --------------------------------------------------------------------------- background thread

_lock = threading.Lock()
_wake = threading.Event()
_thread = None
_last_poll = 0.0


def kick():
    """Make sure the delivery thread is running and looks at the queue now."""
    global _thread
    if not has_app_context() or current_app.testing:
        return
    app = current_app._get_current_object()
    with _lock:
        _wake.set()
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, args=(app,), name="scholar-sync-events", daemon=True)
            _thread.start()


def _loop(app):
    global _thread
    while True:
        try:
            with app.app_context():
                pause = deliver_due()
        except Exception:  # never let the thread die silently with work waiting
            log.exception("Delivering sync events failed")
            pause = 30
            try:
                db.session.remove()
            except Exception:
                pass
        with _lock:
            if pause is None and not _wake.is_set():
                _thread = None
                return
        _wake.wait(timeout=max(1.0, min(pause if pause is not None else 1.0, 60.0)))
        _wake.clear()


def poll_if_idle():
    """Called after requests: once a minute make sure retries of earlier failures are not forgotten
    (this worker process may have been restarted since)."""
    global _last_poll
    now = time.monotonic()
    if now - _last_poll >= 60:
        _last_poll = now
        kick()
