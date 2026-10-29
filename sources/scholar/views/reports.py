from flask import Blueprint, abort, render_template, request

from ..analytics import LEVEL_LABELS, course_mastery, standard_class_summary, student_mastery
from ..extensions import db
from ..grading import counted_attempts, enforce_deadlines
from ..models import Course, User
from ..utils import csv_response, fmt_num, require_course_manager, safe_filename, staff_required

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _course(course_id):
    course = db.session.get(Course, course_id)
    if course is None:
        abort(404)
    require_course_manager(course)
    return course


@bp.route("/course/<int:course_id>/gradebook")
@staff_required
def gradebook(course_id):
    course = _course(course_id)
    assessments = [a for a in course.assessments if a.status != "draft"]
    for a in assessments:
        enforce_deadlines([t for t in a.attempts if t.status == "in_progress"])
    counted = {a.id: counted_attempts(a) for a in assessments}
    students = course.students
    rows = []
    for student in students:
        cells = []
        earned = possible = 0.0
        for a in assessments:
            attempt = counted[a.id].get(student.id)
            cells.append(attempt)
            if attempt:
                earned += attempt.score
                possible += attempt.max_score
        rows.append({"user": student, "cells": cells, "overall": (100.0 * earned / possible) if possible else None})
    averages = []
    for a in assessments:
        values = [t.percent for uid, t in counted[a.id].items() if uid in {s.id for s in students}]
        averages.append(sum(values) / len(values) if values else None)
    if request.args.get("format") == "csv":
        header = ["username", "full_name", "student_number"] + [f"{a.title} (%)" for a in assessments] + ["overall (%)"]
        data = [
            [r["user"].username, r["user"].full_name, r["user"].student_number]
            + [f"{c.percent:.1f}" if c else "" for c in r["cells"]]
            + ["" if r["overall"] is None else f"{r['overall']:.1f}"]
            for r in rows
        ]
        return csv_response(f"{safe_filename(course.code)}_gradebook.csv", header, data)
    return render_template("reports/gradebook.html", course=course, assessments=assessments, rows=rows, averages=averages)


@bp.route("/course/<int:course_id>/mastery")
@staff_required
def mastery(course_id):
    course = _course(course_id)
    standards, per_user = course_mastery(course)
    students = course.students
    summary = standard_class_summary(standards, per_user, [s.id for s in students])
    if request.args.get("format") == "csv":
        header = ["username", "full_name", "student_number"]
        for std in standards:
            header += [f"{std.code} (%)", f"{std.code} level"]
        data = []
        for student in students:
            line = [student.username, student.full_name, student.student_number]
            for std in standards:
                stat = per_user.get(student.id, {}).get(std.id)
                line += [fmt_num(stat["percent"], 1) if stat and stat["percent"] is not None else "", LEVEL_LABELS[stat["level"]] if stat else LEVEL_LABELS["none"]]
            data.append(line)
        return csv_response(f"{safe_filename(course.code)}_standards_mastery.csv", header, data)
    return render_template(
        "reports/mastery.html", course=course, standards=standards, per_user=per_user, students=students, summary=summary
    )


@bp.route("/course/<int:course_id>/student/<int:user_id>")
@staff_required
def student(course_id, user_id):
    course = _course(course_id)
    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    rows = []
    for assessment in course.assessments:
        if assessment.status == "draft":
            continue
        attempts = sorted((a for a in assessment.attempts if a.user_id == user.id), key=lambda a: a.number)
        rows.append({"assessment": assessment, "attempts": attempts, "counted": counted_attempts(assessment).get(user.id)})
    standards, stats = student_mastery(user, [course])
    return render_template("reports/student.html", course=course, user=user, rows=rows, standards=standards, stats=stats)
