"""Security of the staff site: sign-in throttling, two-step sign-in, password rules and session control.

Two-step sign-in is TOTP (RFC 6238: six digits, 30 seconds, SHA-1), which every authenticator app
understands, implemented here with the standard library so the server needs no extra package.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import struct
import time
from datetime import timedelta
from urllib.parse import quote

from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db
from .models import LoginFailure, utcnow

log = logging.getLogger("scholar.auth")

# --------------------------------------------------------------------------- sign-in throttling
#
# fail2ban (installed by the YunoHost package) bans addresses at the firewall. These limits work
# inside the app as well, so they hold on servers without fail2ban and against attackers who
# rotate addresses. One address guessing at one account is stopped quickly; the limit across all
# addresses is higher so that an attacker can't cheaply lock a teacher out of their own account.

WINDOW = timedelta(minutes=15)
PER_ACCOUNT_AND_IP = 5
PER_ACCOUNT = 20
PER_IP = 40
KEEP_FAILURES = timedelta(days=30)

# Checking a password against this costs the same time as checking a real one, so an unknown
# username can't be told from a wrong password by how long the answer takes.
_DUMMY_HASH = generate_password_hash("scholar-no-such-user")


def burn_time(password):
    check_password_hash(_DUMMY_HASH, password or "")


def _count(since, **filters):
    return LoginFailure.query.filter(LoginFailure.created_at >= since).filter_by(**filters).count()


def throttled(username, ip):
    """Minutes to wait before this username may be tried again from this address (0 = go ahead)."""
    since = utcnow() - WINDOW
    name = (username or "").lower()[:120]
    over = (
        (name and _count(since, username=name, ip=ip) >= PER_ACCOUNT_AND_IP)
        or (name and _count(since, username=name) >= PER_ACCOUNT)
        or _count(since, ip=ip) >= PER_IP
    )
    if not over:
        return 0
    query = LoginFailure.query.filter(LoginFailure.created_at >= since)
    oldest = query.filter((LoginFailure.username == name) | (LoginFailure.ip == ip)).order_by(LoginFailure.created_at).first()
    left = (oldest.created_at + WINDOW - utcnow()).total_seconds() if oldest else WINDOW.total_seconds()
    return max(1, int(left // 60) + 1)


def record_failure(username, ip, kind="password"):
    db.session.add(LoginFailure(username=(username or "").lower()[:120], ip=ip or "", kind=kind))
    if secrets.randbelow(50) == 0:
        LoginFailure.query.filter(LoginFailure.created_at < utcnow() - KEEP_FAILURES).delete(synchronize_session=False)


def clear_failures(username, ip):
    """A successful sign-in ends the count for this account from this address."""
    LoginFailure.query.filter_by(username=(username or "").lower()[:120], ip=ip or "").delete(synchronize_session=False)


# --------------------------------------------------------------------------- passwords

MIN_PASSWORD = 10
COMMON_PASSWORDS = {
    "password", "password1", "password12", "password123", "passw0rd", "1234567890", "12345678910", "0123456789",
    "qwertyuiop", "qwerty12345", "qwerty123456", "letmein123", "iloveyou123", "welcome123", "admin12345", "administrator",
    "changeme123", "scholar123", "teacher123", "instructor123", "mindaccel", "mindaccel123", "abcdefghij", "abc1234567",
    "1q2w3e4r5t", "1qaz2wsx3edc", "zaq12wsxcde3", "monkey12345", "dragon12345", "football123", "baseball123", "sunshine123",
    "princess123", "superman123", "trustno1234", "aaaaaaaaaa", "1111111111", "0000000000",
}


def password_problem(password, user=None):
    """Why a new password is not acceptable, or None when it is."""
    if len(password) < MIN_PASSWORD:
        return f"The new password must be at least {MIN_PASSWORD} characters long."
    if len(password) > 256:
        return "The new password is too long."
    lowered = password.lower()
    if lowered in COMMON_PASSWORDS or len(set(lowered)) < 4:
        return "That password is too easy to guess. Choose a longer or less common one."
    if lowered.isdigit() and len(lowered) < 14:
        return "A password made only of digits must be at least 14 digits long."
    if user is not None:
        for own in (user.username, user.email.split("@")[0], *user.full_name.split()):
            if own and len(own) >= 4 and own.lower() in lowered:
                return "Don't use your name, username or email address in your password."
    return None


# --------------------------------------------------------------------------- two-step sign-in (TOTP)

STEP = 30
DIGITS = 6
RECOVERY_CODES = 10


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _code_at(secret, step):
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_code(secret, at=None):
    return _code_at(secret, int((at if at is not None else time.time()) // STEP))


def matching_step(secret, code, at=None, after=0):
    """The time step a code is valid for (now, or one step either side for clock drift), newer than
    ``after`` so a code that was already used is refused. None when the code is wrong."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    now = int((at if at is not None else time.time()) // STEP)
    found = None
    for step in (now - 1, now, now + 1):
        if hmac.compare_digest(_code_at(secret, step), code) and step > after:
            found = step  # no early exit: every candidate is compared
    return found


def provisioning_uri(secret, username, issuer="Scholar"):
    # Kept short on purpose (six digits, 30 seconds and SHA-1 are what apps assume anyway):
    # a short address makes a coarse QR code that every phone camera reads.
    return f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}&issuer={quote(issuer)}"


def qr_data_uri(text):
    """The text as a QR code (PNG data URI). "" where the image library isn't installed, or if
    the code it drew can't be read back: the page then shows the key to type in instead."""
    try:
        import cv2

        image = cv2.QRCodeEncoder_create().encode(text)
        image = cv2.resize(image, None, fx=6, fy=6, interpolation=cv2.INTER_NEAREST)
        image = cv2.copyMakeBorder(image, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
        if cv2.QRCodeDetector().detectAndDecode(image)[0] != text:
            return ""
        ok, png = cv2.imencode(".png", image)
        return "data:image/png;base64," + base64.b64encode(png.tobytes()).decode() if ok else ""
    except Exception:  # no OpenCV (32-bit ARM) or an old one without the encoder
        return ""


def spaced(secret):
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def _hash_recovery(code):
    return hashlib.sha256(_clean_recovery(code).encode()).hexdigest()


def _clean_recovery(code):
    return "".join(ch for ch in str(code or "").lower() if ch.isalnum())


def new_recovery_codes(user):
    """Replace the user's recovery codes. Returns the new codes, to be shown once."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(10)) for _ in range(RECOVERY_CODES)]
    user.recovery_codes = json.dumps([_hash_recovery(c) for c in codes])
    return [f"{c[:5]}-{c[5:]}" for c in codes]


def recovery_codes_left(user):
    try:
        return len(json.loads(user.recovery_codes or "[]"))
    except ValueError:
        return 0


def use_recovery_code(user, code):
    """True when the code is one of the user's unused recovery codes; it is used up."""
    if len(_clean_recovery(code)) != 10:
        return False
    try:
        stored = json.loads(user.recovery_codes or "[]")
    except ValueError:
        return False
    wanted = _hash_recovery(code)
    match = None
    for item in stored:
        if hmac.compare_digest(item, wanted):
            match = item
    if match is None:
        return False
    stored.remove(match)
    user.recovery_codes = json.dumps(stored)
    return True


def check_second_step(user, code):
    """Verify an authenticator or recovery code. Returns "totp", "recovery" or None."""
    step = matching_step(user.totp_secret, code, after=user.totp_last_step or 0)
    if step is not None:
        user.totp_last_step = step
        return "totp"
    if use_recovery_code(user, code):
        return "recovery"
    return None


def turn_off_two_step(user):
    user.totp_enabled = False
    user.totp_secret = ""
    user.totp_last_step = 0
    user.recovery_codes = "[]"


# --------------------------------------------------------------------------- sessions


def sign_out_everywhere(user):
    """Every browser signed in as this user is signed out at its next request."""
    user.session_version = (user.session_version or 0) + 1
