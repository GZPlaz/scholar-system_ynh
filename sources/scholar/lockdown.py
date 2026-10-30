"""Lockdown browser support: Safe Exam Browser (SEB).

When an assessment requires SEB, Scholar generates the SEB configuration file for it and only
lets a browser into the test when it proves it runs that configuration. SEB proves it with the
Config Key (a SHA-256 over the configuration, see
https://safeexambrowser.org/developer/seb-config-key.html), which it mixes with the URL of each
request:

- SEB for Windows sends it in the ``X-SafeExamBrowser-ConfigKeyHash`` header of every request;
- SEB for macOS and iOS can't add headers, so a script on the page reads it from SEB's
  JavaScript API (``SafeExamBrowser.security.configKey``) and posts it to Scholar.

Either way the value is SHA256(page URL without fragment + Config Key), hex. Teachers can also
list extra keys: Config Keys of their own SEB configurations, or Browser Exam Keys (which also
cover the SEB program itself), checked against ``X-SafeExamBrowser-RequestHash`` / the
``browserExamKey`` value.
"""
import hashlib
import hmac
import json
import plistlib
import re

SEB_CONFIG_HEADER = "X-SafeExamBrowser-ConfigKeyHash"
SEB_EXAM_KEY_HEADER = "X-SafeExamBrowser-RequestHash"
SEB_DOWNLOAD_URL = "https://safeexambrowser.org/download_en.html"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def hash_quit_password(password):
    """SEB stores the quit password as a lowercase SHA-256 hex string."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def seb_settings(assessment, start_url, quit_url):
    """The SEB settings for one assessment. Every key here is part of its Config Key."""
    settings = {
        "originatorVersion": "Scholar",  # metadata, excluded from the Config Key
        "sebConfigPurpose": 0,  # starting an exam
        "startURL": start_url,
        "sendBrowserExamKey": True,
        "browserViewMode": 1,  # full screen
        "allowQuit": True,
        "quitURL": quit_url,
        "quitURLConfirm": False,
        "allowPreferencesWindow": False,
        "allowSpellCheck": False,
        "allowVirtualMachine": False,
        "enableRightMouse": False,
        "enablePrintScreen": False,
        "browserWindowAllowReload": True,
        "showReloadButton": True,
        "showTime": True,
        "examSessionClearCookiesOnStart": True,
        "examSessionClearCookiesOnEnd": True,
        "URLFilterEnable": False,
    }
    if assessment.seb_quit_password_hash:
        settings["hashedQuitPassword"] = assessment.seb_quit_password_hash
    return settings


def config_file(settings):
    """An unencrypted .seb file: an XML property list (what SEB's own config tool can save)."""
    return plistlib.dumps(settings, fmt=plistlib.FMT_XML, sort_keys=True)


def _seb_json_value(value):
    if isinstance(value, dict):
        items = [(k, v) for k, v in value.items() if k != "originatorVersion"]
        items = [(k, v) for k, v in items if not (isinstance(v, dict) and not v)]  # empty dicts are dropped
        items.sort(key=lambda kv: kv[0].casefold())  # culture-invariant, case-insensitive
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{_seb_json_value(v)}" for k, v in items) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_seb_json_value(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 15))
    if isinstance(value, (bytes, bytearray)):
        import base64

        return json.dumps(base64.b64encode(bytes(value)).decode("ascii"))
    return json.dumps(str(value), ensure_ascii=False)


def seb_json(settings):
    """The "SEB-JSON" string of the Config Key specification: sorted keys, no whitespace, no escaping."""
    text = _seb_json_value(settings)
    # json.dumps escapes "/" never and "\\" always; the specification wants backslashes unescaped
    return text.replace("\\\\", "\\")


def config_key(settings):
    return hashlib.sha256(seb_json(settings).encode("utf-8")).hexdigest()


def extra_keys(text):
    """Teacher-supplied keys, one 64-character hex value per line (anything else is ignored)."""
    keys = []
    for line in (text or "").splitlines():
        value = line.split("#", 1)[0].strip().lower()
        if HEX64.match(value):
            keys.append(value)
    return keys


def url_hash(url, key):
    return hashlib.sha256((url.split("#", 1)[0] + key).encode("utf-8")).hexdigest()


def matches(url, value, keys):
    value = (value or "").strip().lower()
    if not HEX64.match(value):
        return False
    return any(hmac.compare_digest(url_hash(url, key), value) for key in keys)


def verify(url, config_key_hash, exam_key_hash, keys):
    """True when either SEB value proves one of ``keys`` for ``url``."""
    return matches(url, config_key_hash, keys) or matches(url, exam_key_hash, keys)
