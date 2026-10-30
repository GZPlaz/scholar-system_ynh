"""Test-portal security: test codes, network allowlists, throttling and portal session tokens."""
import hashlib
import hmac
import ipaddress
import logging
import math
import secrets
from datetime import timedelta, timezone

from flask import current_app, request

from .extensions import db
from .models import Assessment, AttemptEvent, PortalFailure, utcnow

log = logging.getLogger("scholar.portal")

# No 0/O, 1/I/L: codes are read off a board or a slip and typed on a phone.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8  # 31^8 ≈ 8.5e11 codes; guessing is throttled on top of that
CUSTOM_CODE_MIN = 6

PORTAL_COOKIE = "scholar_test"
CONFIRM_TTL = timedelta(minutes=10)  # time to confirm identity after entering code and ID
SESSION_TTL = timedelta(hours=12)

# Throttling. Whole classrooms usually share one public IP address, so per-address limits are
# generous and nothing here bans an address at the firewall; PIN guessing is limited per student.
LOOKUP_FAILURES_PER_IP = 60  # wrong code/ID combinations per address per window
PIN_FAILURES_PER_STUDENT = 5  # wrong PINs per student and test per window
THROTTLE_WINDOW = timedelta(minutes=15)


# --------------------------------------------------------------------------- codes


def normalize_code(value):
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())[:60]


def generate_code():
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        if not Assessment.query.filter_by(access_code=code).first():
            return code


def validate_custom_code(raw, assessment=None):
    """Returns (code, error)."""
    code = normalize_code(raw)
    if len(code) < CUSTOM_CODE_MIN:
        return None, f"A test code needs at least {CUSTOM_CODE_MIN} letters or digits."
    clash = Assessment.query.filter(Assessment.access_code == code)
    if assessment is not None and assessment.id:
        clash = clash.filter(Assessment.id != assessment.id)
    if clash.first():
        return None, "Another assessment already uses this test code."
    return code, None


def ensure_code(assessment):
    if len(assessment.access_code or "") < CUSTOM_CODE_MIN:
        assessment.access_code = generate_code()
    return assessment.access_code


def normalize_student_id(value):
    return "".join(str(value or "").split()).casefold()[:64]


# --------------------------------------------------------------------------- networks


def parse_networks(text):
    """Returns (networks, errors) for an allowlist with one IP address or CIDR range per line."""
    networks, errors = [], []
    for line in (text or "").splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            errors.append(f"“{entry}” is not an IP address or a range such as 192.0.2.0/24.")
    return networks, errors


def ip_allowed(ip, allowlist_text):
    networks, _ = parse_networks(allowlist_text)
    if not networks:
        return True
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in net for net in networks if net.version == address.version)


# --------------------------------------------------------------------------- throttling


def client_ip():
    return request.remote_addr or ""


def record_failure(scope=""):
    db.session.add(PortalFailure(ip=client_ip(), scope=scope))
    # Never use the "Failed login attempt" wording here: fail2ban would ban a classroom's shared address.
    log.warning("Test portal: rejected %s from %s", scope.split(":", 1)[0] or "attempt", client_ip())


def failures(ip=None, scope=None):
    since = utcnow() - THROTTLE_WINDOW
    query = PortalFailure.query.filter(PortalFailure.created_at >= since)
    if ip is not None:
        query = query.filter(PortalFailure.ip == ip)
    if scope is not None:
        query = query.filter(PortalFailure.scope == scope)
    return query.count()


def lookup_blocked():
    return failures(ip=client_ip(), scope="lookup") >= LOOKUP_FAILURES_PER_IP


def pin_scope(assessment_id, user_id):
    return f"pin:{assessment_id}:{user_id}"


def pin_blocked(assessment_id, user_id):
    return failures(scope=pin_scope(assessment_id, user_id)) >= PIN_FAILURES_PER_STUDENT


def clear_pin_failures(assessment_id, user_id):
    PortalFailure.query.filter_by(scope=pin_scope(assessment_id, user_id)).delete()


def prune_failures():
    PortalFailure.query.filter(PortalFailure.created_at < utcnow() - timedelta(days=1)).delete()


# --------------------------------------------------------------------------- portal session tokens


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def new_token():
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def cookie_path():
    return current_app.config.get("APP_PREFIX") or "/"


def set_portal_cookie(response, token):
    response.set_cookie(
        PORTAL_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        path=cookie_path(),
        secure=bool(current_app.config.get("SESSION_COOKIE_SECURE")) or request.is_secure,
        httponly=True,
        samesite="Strict",
    )
    return response


def clear_portal_cookie(response):
    response.delete_cookie(PORTAL_COOKIE, path=cookie_path())
    return response


# --------------------------------------------------------------------------- integrity events

MAX_EVENTS_PER_ATTEMPT = 500  # a misbehaving browser cannot flood the timeline


def add_event(attempt, kind, detail=""):
    if attempt is None or len(attempt.events) >= MAX_EVENTS_PER_ATTEMPT:
        return None
    # created_at is set now (not at flush) so events added in this request can be compared at once
    event = AttemptEvent(attempt=attempt, kind=kind[:40], detail=str(detail)[:300], ip=client_ip(), created_at=utcnow())
    db.session.add(event)
    return event


# --------------------------------------------------------------------------- start passwords


def _rotating_code(secret, window):
    digest = hmac.new(secret.encode(), str(window).encode(), hashlib.sha256).digest()
    return "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in digest[:6])


def rotating_password(assessment, now=None):
    """The current rotating password and the seconds until it changes (the proctor shows it in the room)."""
    period = max(1, assessment.password_rotation_minutes or 5) * 60
    stamp = int((now or utcnow()).replace(tzinfo=timezone.utc).timestamp())
    window = stamp // period
    return _rotating_code(assessment.password_secret or "", window), period - stamp % period


def check_access_password(assessment, given, now=None):
    """Static: the teacher's word (case and spaces ignored). Rotating: the current or previous code."""
    mode = assessment.access_password_mode
    if not mode:
        return True
    given = normalize_code(given)
    if not given:
        return False
    if mode == "static":
        return hmac.compare_digest(given, normalize_code(assessment.access_password))
    period = max(1, assessment.password_rotation_minutes or 5) * 60
    stamp = int((now or utcnow()).replace(tzinfo=timezone.utc).timestamp())
    window = stamp // period
    # the previous code stays valid for one more period, so a code read just before it changes still works
    return any(hmac.compare_digest(given, _rotating_code(assessment.password_secret or "", w)) for w in (window, window - 1))


def password_scope(assessment_id, user_id):
    return f"password:{assessment_id}:{user_id}"


PASSWORD_FAILURES_PER_STUDENT = 10


def password_blocked(assessment_id, user_id):
    return failures(scope=password_scope(assessment_id, user_id)) >= PASSWORD_FAILURES_PER_STUDENT


# --------------------------------------------------------------------------- pausing


def lock_attempt(attempt, reason, detail=""):
    if attempt.locked:
        return
    attempt.locked = True
    attempt.lock_reason = reason[:200]
    attempt.locked_at = utcnow()
    add_event(attempt, "locked", detail or reason)


def unlock_attempt(attempt, detail=""):
    """Let the student continue; give back the paused time when the assessment stops the clock."""
    if not attempt.locked:
        return 0
    given = 0
    if attempt.assessment.clock_stops_when_paused and attempt.locked_at:
        given = math.ceil((utcnow() - attempt.locked_at).total_seconds() / 60)
        attempt.extra_minutes += given
    attempt.locked = False
    attempt.lock_reason = ""
    attempt.locked_at = None
    add_event(attempt, "unlocked", detail + (f", {given} paused minute(s) given back" if given else ""))
    return given


# --------------------------------------------------------------------------- accommodations


def accommodation_for(assessment, user):
    from .models import Accommodation

    return Accommodation.query.filter_by(assessment_id=assessment.id, user_id=user.id).first()
