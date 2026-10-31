"""Tests made from a PDF: the teacher uploads the test as it is and types in the correct answers.

The PDF is drawn page by page into JPEG images (students see those, never the file). Each numbered
question becomes an ordinary Scholar question without text of its own ("Question 7"): just its
type and its correct answer, so grading, analytics, paper answer sheets and scanning all work
unchanged. The questions are marked with the topic "PDF: <file name>" and kept out of the
question bank; this module is the only thing that creates, edits or deletes them.
"""
import hashlib
import re

from .extensions import db
from .models import AssessmentItem, AssessmentPdf, Choice, PdfPage, Question
from .utils import parse_float

MAX_PAGES = 60
MAX_QUESTIONS = 300
RENDER_DPI = 150
LONGEST_SIDE = 2300  # pixels; a poster-sized page is drawn smaller than 150 dpi
JPEG_QUALITY = 85
TOPIC_PREFIX = "PDF: "

KEY_TYPES = {
    "mc": "One answer",
    "ms": "Select all that apply",
    "tf": "True / False",
    "numeric": "A number",
    "short": "A word or phrase",
    "essay": "Graded by hand",
}
CHOICE_TYPES = ("mc", "ms", "tf")


class PdfProblem(Exception):
    """Something wrong with the uploaded file; the message is meant for the teacher."""


class KeyProblem(Exception):
    """The answer key can't be saved as it is; the message is meant for the teacher."""


def available():
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


# --------------------------------------------------------------------------- the PDF


def render(data):
    """Draw every page. Returns [{"page": 1, "width": w, "height": h, "jpeg": bytes}, …]."""
    try:
        import cv2
        import numpy as np
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - depends on the server
        raise PdfProblem("Tests from a PDF are not available on this server (the PDF library is not installed).") from exc
    if b"%PDF-" not in data[:1024]:
        raise PdfProblem("This file is not a PDF. Choose the test as a .pdf file.")
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:  # PdfiumError, but its type moves between versions
        if "password" in str(exc).lower():
            raise PdfProblem("This PDF is password-protected. Remove the password and upload it again.") from exc
        raise PdfProblem("Scholar could not open this PDF. It may be damaged: try saving or printing it to a new PDF.") from exc
    count = len(document)
    if count == 0:
        raise PdfProblem("This PDF has no pages.")
    if count > MAX_PAGES:
        raise PdfProblem(f"This PDF has {count} pages; tests can have at most {MAX_PAGES}.")
    pages = []
    for number in range(count):
        page = document[number]
        width, height = page.get_size()
        scale = min(RENDER_DPI / 72.0, LONGEST_SIDE / max(width, height))
        bitmap = page.render(scale=scale).to_numpy()
        image = bitmap[:, :, :3] if bitmap.ndim == 3 and bitmap.shape[2] > 3 else bitmap
        ok, buffer = cv2.imencode(".jpg", np.ascontiguousarray(image), [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
        if not ok:
            raise PdfProblem(f"Page {number + 1} could not be drawn.")
        pages.append({"page": number + 1, "width": image.shape[1], "height": image.shape[0], "jpeg": buffer.tobytes()})
    return pages


def attach_pdf(assessment, filename, data):
    """Store the file and its page images on the assessment, replacing any earlier PDF."""
    pages = render(data)
    assessment.pdf = None
    assessment.pdf_pages = []
    db.session.flush()  # the old rows go first: pages are unique per assessment
    assessment.pdf = AssessmentPdf(
        filename=(filename or "test.pdf")[:255], size=len(data), sha256=hashlib.sha256(data).hexdigest(), pages=len(pages), data=data
    )
    assessment.pdf_pages = [PdfPage(page=p["page"], width=p["width"], height=p["height"], data=p["jpeg"]) for p in pages]
    return len(pages)


def clone_pdf(source, copy, user):
    """Copy a PDF test: the file, its pages, and its own questions (a key must never be shared)."""
    copy.pdf = AssessmentPdf(
        filename=source.pdf.filename, size=source.pdf.size, sha256=source.pdf.sha256, pages=source.pdf.pages, data=bytes(source.pdf.data)
    )
    copy.pdf_pages = [PdfPage(page=p.page, width=p.width, height=p.height, data=bytes(p.data)) for p in source.pdf_pages]
    for item in sorted(source.items, key=lambda i: (i.position, i.id)):
        q = item.question
        clone = Question(
            owner=user, qtype=q.qtype, stem=q.stem, points=q.points, topic=q.topic, shared=False, archived=True,
            partial_credit=q.partial_credit, case_sensitive=q.case_sensitive,
            numeric_answer=q.numeric_answer, numeric_tolerance=q.numeric_tolerance,
            choices=[Choice(text=c.text, is_correct=c.is_correct, position=c.position) for c in q.choices],
        )
        db.session.add(clone)
        copy.items.append(AssessmentItem(question=clone, position=item.position, points=item.points))


def is_pdf_question(question):
    return (question.topic or "").startswith(TOPIC_PREFIX)


def forget_questions(questions):
    """Delete PDF questions that no assessment uses any more (after their assessment is deleted)."""
    for q in questions:
        db.session.expire(q, ["items"])
        if is_pdf_question(q) and not q.items:
            db.session.delete(q)


# --------------------------------------------------------------------------- the answer key


def labels_for(qtype, n_choices):
    return ["T", "F"] if qtype == "tf" else [chr(65 + i) for i in range(n_choices)]


def key_rows(assessment):
    """The saved key as rows for the editor."""
    rows = []
    for number, item in enumerate(sorted(assessment.items, key=lambda i: (i.position, i.id)), start=1):
        q = item.question
        row = {"n": number, "type": q.qtype if q.qtype in KEY_TYPES else "mc", "choices": 4, "answer": "", "points": q.points, "tol": 0.0}
        if q.qtype in CHOICE_TYPES:
            row["choices"] = len(q.choices) or 4
            labels = labels_for(q.qtype, row["choices"])
            row["answer"] = "".join(labels[k] for k, c in enumerate(q.choices) if c.is_correct and k < len(labels))
        elif q.qtype == "numeric":
            row["answer"] = "" if q.numeric_answer is None else f"{q.numeric_answer:g}"
            row["tol"] = q.numeric_tolerance or 0.0
        elif q.qtype == "short":
            row["answer"] = " | ".join(c.text for c in q.choices)
        rows.append(row)
    return rows


def blank_rows(count, qtype="mc", choices=4):
    return [{"n": n, "type": qtype, "choices": choices, "answer": "", "points": 1.0, "tol": 0.0} for n in range(1, count + 1)]


def parse_rows(form):
    """Read the editor's form. Returns (rows, errors); rows are validated, spare empty rows at the end dropped."""
    try:
        count = int(form.get("rows", "0"))
    except ValueError:
        count = 0
    count = max(0, min(count, MAX_QUESTIONS))
    rows = []
    for n in range(1, count + 1):
        qtype = form.get(f"type_{n}", "mc")
        qtype = qtype if qtype in KEY_TYPES else "mc"
        try:
            choices = max(2, min(8, int(form.get(f"choices_{n}", "4"))))
        except ValueError:
            choices = 4
        rows.append(
            {
                "n": n,
                "type": qtype,
                "choices": 2 if qtype == "tf" else choices,
                "answer": (form.get(f"answer_{n}") or "").strip(),
                "points": parse_float(form.get(f"points_{n}"), 1.0),
                "tol": parse_float(form.get(f"tol_{n}"), 0.0) or 0.0,
            }
        )
    while rows and not rows[-1]["answer"] and rows[-1]["type"] != "essay":
        rows.pop()  # spare rows below the last question
    errors = []
    for row in rows:
        n = row["n"]
        if row["points"] is None or row["points"] <= 0:
            errors.append(f"Question {n}: points must be more than 0.")
            continue
        qtype, raw = row["type"], row["answer"]
        if qtype in ("mc", "tf"):
            word = raw.upper()
            if qtype == "tf":
                word = {"TRUE": "T", "FALSE": "F"}.get(word, word)
            letters = labels_for(qtype, row["choices"])
            if word not in letters:
                errors.append(f"Question {n}: choose the correct answer ({' or '.join(letters) if qtype == 'tf' else 'A to ' + letters[-1]}).")
                continue
            row["correct"] = [letters.index(word)]
        elif qtype == "ms":
            letters = sorted(set(re.findall(r"[A-Z]", raw.upper())))
            valid = labels_for("ms", row["choices"])
            if not letters or any(x not in valid for x in letters):
                errors.append(f"Question {n}: choose every correct answer (letters A to {valid[-1]}).")
                continue
            row["correct"] = [valid.index(x) for x in letters]
        elif qtype == "numeric":
            value = parse_float(raw)
            if value is None:
                errors.append(f"Question {n}: type the correct number.")
                continue
            if row["tol"] < 0:
                errors.append(f"Question {n}: the allowed error can't be negative.")
                continue
            row["correct"] = value
        elif qtype == "short":
            accepted = [a.strip() for a in re.split(r"[|;\n]", raw) if a.strip()]
            if not accepted:
                errors.append(f"Question {n}: type the correct answer (separate several accepted answers with |).")
                continue
            row["correct"] = accepted
        else:
            row["correct"] = None
    return rows, errors


def _desired_choices(row):
    if row["type"] == "tf":
        return [("True", 0 in row["correct"]), ("False", 1 in row["correct"])]
    if row["type"] in ("mc", "ms"):
        return [(chr(65 + k), k in row["correct"]) for k in range(row["choices"])]
    if row["type"] == "short":
        return [(text, True) for text in row["correct"]]
    return []


def _apply(question, row, topic):
    question.qtype = row["type"]
    question.stem = f"Question {row['n']}"
    question.points = row["points"]
    question.topic = topic
    question.explanation = ""
    question.numeric_answer = row["correct"] if row["type"] == "numeric" else None
    question.numeric_tolerance = row["tol"] if row["type"] == "numeric" else 0.0
    desired = _desired_choices(row)
    if [c.text for c in question.choices] == [text for text, _ in desired]:
        for choice, (_, correct) in zip(question.choices, desired):
            choice.is_correct = correct
    else:
        question.choices = [Choice(text=text, is_correct=correct, position=k) for k, (text, correct) in enumerate(desired)]


def save_key(assessment, rows, user):
    """Make the assessment's questions match the key rows. Raises KeyProblem when that isn't allowed."""
    items = sorted(assessment.items, key=lambda i: (i.position, i.id))
    if assessment.is_locked:
        if len(items) != len(rows):
            raise KeyProblem("Students have already started this test, so questions can't be added or removed. You can still correct answers and points.")
        for item, row in zip(items, rows):
            q = item.question
            if q.qtype != row["type"] or (q.qtype in CHOICE_TYPES and len(q.choices) != row["choices"]):
                raise KeyProblem(f"Question {row['n']}: its type and number of choices can't change after students have started.")
    topic = (TOPIC_PREFIX + (assessment.pdf.filename if assessment.pdf else "test"))[:120]
    for index, row in enumerate(rows):
        item = items[index] if index < len(items) else None
        if item is None:
            question = Question(owner=user, shared=False, archived=True, stem="", qtype=row["type"], points=row["points"])
            db.session.add(question)
            item = AssessmentItem(question=question, position=index + 1)
            assessment.items.append(item)  # (SQLAlchemy 2 doesn't add it to the session just because it points at the assessment)
        _apply(item.question, row, topic)
        item.position = index + 1
        item.points = None
    removed = []
    for item in items[len(rows):]:
        removed.append(item.question)
        db.session.delete(item)
        assessment.items.remove(item)
    db.session.flush()
    forget_questions(removed)
    return len(rows)
