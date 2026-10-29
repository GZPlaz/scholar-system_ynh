import csv
import io
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Response as FlaskResponse
from flask import abort, current_app, flash, g, request
from flask_login import current_user, login_required

from .extensions import db
from .models import AuditLog, Setting

# --------------------------------------------------------------------------- settings

SETTING_DEFAULTS = {
    "institution_name": None,  # falls back to config DEFAULT_INSTITUTION
    "timezone": None,  # falls back to config DEFAULT_TIMEZONE
    "mastery_threshold": "80",
    "approaching_threshold": "60",
    "ldap_autoprovision": "1",
    "ldap_default_role": "student",
    "attempt_policy": "best",  # best | latest — which attempt counts for reports
    "login_message": "",
}


def get_setting(key):
    cache = g.setdefault("_settings_cache", {})
    if key not in cache:
        row = db.session.get(Setting, key)
        if row is not None:
            cache[key] = row.value
        else:
            default = SETTING_DEFAULTS.get(key)
            if default is None and key == "institution_name":
                default = current_app.config["DEFAULT_INSTITUTION"]
            if default is None and key == "timezone":
                default = current_app.config["DEFAULT_TIMEZONE"]
            cache[key] = default if default is not None else ""
    return cache[key]


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    g.setdefault("_settings_cache", {})[key] = str(value)


def thresholds():
    return parse_float(get_setting("mastery_threshold"), 80.0), parse_float(get_setting("approaching_threshold"), 60.0)


# --------------------------------------------------------------------------- time


def local_zone():
    try:
        return ZoneInfo(get_setting("timezone") or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def utc_to_local(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(local_zone())


def parse_local_datetime(value):
    """Parse an HTML ``datetime-local`` value in the institution timezone → naive UTC."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(value, fmt).replace(tzinfo=local_zone())
            return local.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def to_input_value(dt):
    local = utc_to_local(dt)
    return local.strftime("%Y-%m-%dT%H:%M") if local else ""


def format_dt(dt, fmt="%b %d, %Y %H:%M"):
    local = utc_to_local(dt)
    return local.strftime(fmt) if local else "—"


# --------------------------------------------------------------------------- parsing


def parse_int(value, default=None):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_float(value, default=None):
    try:
        result = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def parse_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def fmt_num(value, digits=2):
    if value is None:
        return "—"
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


# --------------------------------------------------------------------------- access control


def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if current_user.role not in roles:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


staff_required = roles_required("admin", "instructor")
admin_required = roles_required("admin")


def can_manage_course(course, user=None):
    user = user or current_user
    return user.is_authenticated and (user.is_admin or course.has_instructor(user))


def require_course_manager(course):
    if not can_manage_course(course):
        abort(403)


def can_edit_question(question, user=None):
    user = user or current_user
    return user.is_admin or question.owner_id == user.id


def can_view_question(question, user=None):
    user = user or current_user
    return user.is_staff and (question.shared or can_edit_question(question, user))


# --------------------------------------------------------------------------- audit


def client_ip():
    return request.remote_addr or ""


def log_event(action, detail=""):
    db.session.add(
        AuditLog(
            user_id=current_user.id if current_user and current_user.is_authenticated else None,
            action=action,
            detail=detail[:2000],
            ip=client_ip(),
        )
    )


# --------------------------------------------------------------------------- csv


def read_csv_upload(field="file"):
    """Return a list of dict rows (lower-cased stripped headers) or None after flashing an error."""
    upload = request.files.get(field)
    text = ""
    if upload and upload.filename:
        raw = upload.read()
        for encoding in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    elif request.form.get("csv_text", "").strip():
        text = request.form["csv_text"]
    if not text.strip():
        flash("Please choose a CSV file (or paste CSV text).", "error")
        return None
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        flash("The CSV file has no header row.", "error")
        return None
    rows = []
    for row in reader:
        clean = {(k or "").strip().lower(): (v or "").strip() if isinstance(v, str) else "" for k, v in row.items()}
        if any(clean.values()):
            rows.append(clean)
    return rows


def csv_response(filename, header, rows):
    buf = io.StringIO()
    buf.write("﻿")  # Excel-friendly BOM
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
    return FlaskResponse(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def safe_filename(text):
    keep = "".join(c if c.isalnum() or c in "-_" else "_" for c in text)
    return keep.strip("_")[:80] or "export"
