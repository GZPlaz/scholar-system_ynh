"""Redirects from the addresses used before the site was reorganised.

The site root became the student test portal and everything for staff moved to /staff, so
bookmarks such as /courses/3 or /test/ keep working.
"""
from flask import Blueprint, redirect, request

bp = Blueprint("legacy", __name__)

OLD_STAFF = "courses,assessments,questions,standards,admin,reports,account,login"


def _go(path):
    target = path + ("?" + request.query_string.decode() if request.query_string else "")
    return redirect(target, 302)


@bp.route("/<any(%s):first>" % OLD_STAFF)
@bp.route("/<any(%s):first>/" % OLD_STAFF)
@bp.route("/<any(%s):first>/<path:rest>" % OLD_STAFF)
def old_staff(first, rest=""):
    # /staff/login and /staff/account have no trailing slash; the section index pages do
    tail = f"/{rest}" if rest else ("" if first in ("login", "account") else "/")
    return _go(f"{request.script_root}/staff/{first}{tail}")


@bp.route("/test")
@bp.route("/test/")
@bp.route("/test/<path:rest>")
def old_portal(rest=""):
    return _go(f"{request.script_root}/{rest}")
