"""Demo data so a fresh install can be explored immediately (``manage.py seed-demo``)."""
import random

from .extensions import db
from .grading import finalize_attempt, save_answer, start_attempt
from .models import Assessment, AssessmentItem, Choice, Course, Enrollment, Question, Standard, User
from .security import generate_code

STANDARDS = [
    ("MATH.ALG.1", "Solve linear equations in one variable", "Algebra"),
    ("MATH.ALG.2", "Interpret the slope and intercept of a linear model", "Algebra"),
    ("MATH.CALC.1", "Compute derivatives of polynomial functions", "Calculus"),
    ("MATH.CALC.2", "Apply the chain rule", "Calculus"),
]

QUESTIONS = [
    ("mc", "Solve for x: 3x + 5 = 20", ["3", "5", "15", "25/3"], [1], "MATH.ALG.1"),
    ("mc", "Solve for x: 2(x − 4) = 10", ["1", "7", "9", "14"], [2], "MATH.ALG.1"),
    ("numeric", "Solve for x: 5x − 7 = 18", None, 5, "MATH.ALG.1"),
    ("mc", "In y = 3x + 2, what does 3 represent?", ["The y-intercept", "The slope", "The x-intercept", "The domain"], [1], "MATH.ALG.2"),
    ("tf", "A line with negative slope decreases from left to right.", None, True, "MATH.ALG.2"),
    ("mc", "What is d/dx of x³?", ["x²", "3x²", "3x³", "x⁴/4"], [1], "MATH.CALC.1"),
    ("ms", "Which of these functions have derivative 2x? (select all)", ["x²", "x² + 7", "2x²", "x² − 1"], [0, 1, 3], "MATH.CALC.1"),
    ("short", "What is the derivative of a constant? (one word)", None, ["zero", "0"], "MATH.CALC.1"),
    ("mc", "d/dx of (2x + 1)⁵ is…", ["5(2x + 1)⁴", "10(2x + 1)⁴", "(2x + 1)⁴", "10x⁴"], [1], "MATH.CALC.2"),
    ("essay", "Explain, in your own words, why the chain rule multiplies by the inner derivative.", None, None, "MATH.CALC.2"),
]


def seed():
    standards = {}
    for code, title, subject in STANDARDS:
        std = Standard.query.filter_by(code=code).first() or Standard(code=code, title=title, subject=subject)
        db.session.add(std)
        standards[code] = std

    instructor = User.query.filter_by(username="demo.instructor").first()
    if instructor is None:
        instructor = User(username="demo.instructor", full_name="Dr. Ada Demo", role="instructor", email="ada@example.org")
        instructor.set_password("instructor123")
        db.session.add(instructor)

    course = Course.query.filter_by(code="DEMO101").first()
    if course is None:
        course = Course(code="DEMO101", title="Foundations of Mathematics (demo)", term="Fall 2026")
        course.instructors.append(instructor)
        db.session.add(course)

    names = ["Amara Okafor", "Liam Chen", "Sofia Rossi", "Noah Martin", "Yuki Tanaka", "Omar Haddad", "Elena Petrova", "Lucas Silva"]
    students = []
    for idx, name in enumerate(names, start=1):
        username = f"demo.student{idx}"
        user = User.query.filter_by(username=username).first()
        if user is None:
            # test takers: no password; they use the test portal with their student ID
            user = User(username=username, full_name=name, role="student", auth_source="portal", student_number=f"MA2026{idx:03d}")
            db.session.add(user)
        students.append(user)
        db.session.flush()
        if not Enrollment.query.filter_by(course_id=course.id, user_id=user.id).first():
            db.session.add(Enrollment(course=course, user=user))

    if Assessment.query.filter_by(course=course, title="Unit 1 Check-in").first():
        db.session.commit()
        return

    questions = []
    for qtype, stem, options, key, code in QUESTIONS:
        q = Question(owner=instructor, qtype=qtype, stem=stem, points=4.0 if qtype == "essay" else 1.0, standards=[standards[code]])
        if qtype in ("mc", "ms"):
            q.choices = [Choice(text=o, is_correct=i in key, position=i) for i, o in enumerate(options)]
        elif qtype == "tf":
            q.choices = [Choice(text="True", is_correct=key, position=0), Choice(text="False", is_correct=not key, position=1)]
        elif qtype == "short":
            q.choices = [Choice(text=a, is_correct=True, position=i) for i, a in enumerate(key)]
        elif qtype == "numeric":
            q.numeric_answer = key
        if qtype == "essay":
            q.explanation = "Full credit: mentions composition of functions and the rate of the inner function."
        db.session.add(q)
        questions.append(q)

    assessment = Assessment(
        course=course,
        owner=instructor,
        title="Unit 1 Check-in",
        instructions="Answer every question. Show your reasoning on scratch paper.",
        status="published",
        max_attempts=2,
        show_results="immediately",
        show_correct_answers=True,
        access_code=generate_code(),
    )
    for pos, q in enumerate(questions, start=1):
        assessment.items.append(AssessmentItem(question=q, position=pos))
    db.session.add(assessment)
    db.session.flush()

    # Simulated submissions with varied ability so reports have something to show.
    rng = random.Random(42)
    for ability, student in zip([0.95, 0.85, 0.8, 0.7, 0.6, 0.5, 0.4], students):
        attempt = start_attempt(assessment, student)
        db.session.flush()
        for item in assessment.items:
            q = item.question
            right = rng.random() < ability
            if q.qtype in ("mc", "tf"):
                correct = [c.id for c in q.choices if c.is_correct][0]
                wrong = [c.id for c in q.choices if not c.is_correct]
                answer = correct if right else rng.choice(wrong)
            elif q.qtype == "ms":
                answer = [c.id for c in q.choices if c.is_correct] if right else [q.choices[0].id, q.choices[2].id]
            elif q.qtype == "short":
                answer = "zero" if right else "one"
            elif q.qtype == "numeric":
                answer = "5" if right else "4"
            else:
                answer = "Because the outer function changes at a rate that depends on how fast the inner function changes."
            save_answer(attempt, item, answer)
        finalize_attempt(attempt)
    db.session.commit()
