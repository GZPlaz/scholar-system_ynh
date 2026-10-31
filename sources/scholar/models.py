import json
from datetime import datetime, timedelta, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


def utcnow():
    """Naive UTC timestamp (all datetimes are stored as naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


ROLES = {"admin": "Administrator", "instructor": "Instructor", "student": "Student"}

# Students never sign in to the staff site: they are test takers who identify themselves in the
# test portal (the site root) with an assessment's test code and their student ID (plus a PIN if asked).
IDENTITY_CHECKS = {
    "confirm": "Student confirms their name",
    "pin": "Student confirms their name and enters their personal PIN",
}

QTYPES = {
    "mc": "Multiple choice",
    "ms": "Multiple select",
    "tf": "True / False",
    "short": "Short answer",
    "numeric": "Numeric",
    "essay": "Essay / constructed response",
}
CHOICE_TYPES = ("mc", "ms", "tf")

SHOW_RESULTS = {
    "immediately": "Immediately after submission",
    "after_close": "After the assessment closes",
    "never": "Never (instructor releases manually)",
}


# --------------------------------------------------------------------------- users


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(200), nullable=False, default="")
    email = db.Column(db.String(200), nullable=False, default="")
    student_number = db.Column(db.String(64), nullable=False, default="")
    role = db.Column(db.String(20), nullable=False, default="student")
    auth_source = db.Column(db.String(20), nullable=False, default="local")
    password_hash = db.Column(db.String(256))
    must_change_password = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    # Identity in a connected system, e.g. "rosario:student:42" (set by the integration API)
    external_id = db.Column(db.String(120), index=True)
    # Students only: hash of the personal test-portal PIN (never stored in clear)
    pin_hash = db.Column(db.String(256))
    pin_set_at = db.Column(db.DateTime)

    @property
    def is_active(self):
        return bool(self.active)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return bool(self.password_hash) and check_password_hash(self.password_hash, password)

    def set_pin(self, pin):
        self.pin_hash = generate_password_hash(pin)
        self.pin_set_at = utcnow()

    def check_pin(self, pin):
        return bool(self.pin_hash) and bool(pin) and check_password_hash(self.pin_hash, pin)

    @property
    def can_sign_in(self):
        """Only staff use the staff site; students only ever use the test portal."""
        return self.is_staff and bool(self.active)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def is_staff(self):
        return self.role in ("admin", "instructor")

    @property
    def is_student(self):
        return self.role == "student"

    @property
    def display_name(self):
        return self.full_name or self.username

    @property
    def sort_key(self):
        parts = (self.full_name or self.username).split()
        return ((parts[-1] if parts else "").lower(), (self.full_name or "").lower(), self.username)


# --------------------------------------------------------------------------- courses

course_instructors = db.Table(
    "course_instructors",
    db.Column("course_id", db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True),
    db.Column("user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class Course(db.Model):
    __tablename__ = "courses"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    term = db.Column(db.String(60), nullable=False, default="")
    description = db.Column(db.Text, nullable=False, default="")
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    # e.g. "rosario:cp:15" when the course is linked to a RosarioSIS course period
    external_id = db.Column(db.String(120), index=True)

    instructors = db.relationship("User", secondary=course_instructors, backref="teaching_courses")
    enrollments = db.relationship("Enrollment", back_populates="course", cascade="all, delete-orphan")
    assessments = db.relationship(
        "Assessment", back_populates="course", cascade="all, delete-orphan", order_by="Assessment.created_at"
    )

    @property
    def students(self):
        return sorted((e.user for e in self.enrollments), key=lambda u: u.sort_key)

    @property
    def label(self):
        return f"{self.code} — {self.title}" + (f" ({self.term})" if self.term else "")

    def has_student(self, user):
        return any(e.user_id == user.id for e in self.enrollments)

    def has_instructor(self, user):
        return any(i.id == user.id for i in self.instructors)


class Enrollment(db.Model):
    __tablename__ = "enrollments"
    __table_args__ = (db.UniqueConstraint("course_id", "user_id"),)

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    course = db.relationship("Course", back_populates="enrollments")
    user = db.relationship("User", backref=db.backref("enrollments", cascade="all, delete-orphan"))


# --------------------------------------------------------------------------- standards & questions

question_standards = db.Table(
    "question_standards",
    db.Column("question_id", db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
    db.Column("standard_id", db.Integer, db.ForeignKey("standards.id", ondelete="CASCADE"), primary_key=True),
)


class Standard(db.Model):
    """A learning outcome / standard that questions are aligned to."""

    __tablename__ = "standards"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(60), unique=True, nullable=False)
    title = db.Column(db.String(300), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    subject = db.Column(db.String(120), nullable=False, default="")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    questions = db.relationship("Question", secondary=question_standards, back_populates="standards")


class Question(db.Model):
    __tablename__ = "questions"

    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    qtype = db.Column(db.String(20), nullable=False, default="mc")
    stem = db.Column(db.Text, nullable=False)
    points = db.Column(db.Float, nullable=False, default=1.0)
    explanation = db.Column(db.Text, nullable=False, default="")
    topic = db.Column(db.String(120), nullable=False, default="")
    shared = db.Column(db.Boolean, nullable=False, default=True)
    partial_credit = db.Column(db.Boolean, nullable=False, default=True)
    case_sensitive = db.Column(db.Boolean, nullable=False, default=False)
    numeric_answer = db.Column(db.Float)
    numeric_tolerance = db.Column(db.Float, nullable=False, default=0.0)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    owner = db.relationship("User")
    choices = db.relationship(
        "Choice", back_populates="question", cascade="all, delete-orphan", order_by="Choice.position"
    )
    standards = db.relationship("Standard", secondary=question_standards, back_populates="questions")
    items = db.relationship("AssessmentItem", back_populates="question")

    @property
    def type_label(self):
        return QTYPES.get(self.qtype, self.qtype)

    @property
    def has_choices(self):
        return self.qtype in CHOICE_TYPES

    @property
    def correct_choices(self):
        return [c for c in self.choices if c.is_correct]

    @property
    def stem_preview(self):
        text = " ".join(self.stem.split())
        return text if len(text) <= 140 else text[:137] + "…"


class Choice(db.Model):
    """An answer option (choice types) or an accepted answer (short answer)."""

    __tablename__ = "choices"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    is_correct = db.Column(db.Boolean, nullable=False, default=False)
    position = db.Column(db.Integer, nullable=False, default=0)

    question = db.relationship("Question", back_populates="choices")


# --------------------------------------------------------------------------- assessments


class Assessment(db.Model):
    __tablename__ = "assessments"

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey("courses.id", ondelete="CASCADE"), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    title = db.Column(db.String(200), nullable=False)
    instructions = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="draft")  # draft | published | closed
    time_limit_minutes = db.Column(db.Integer)
    opens_at = db.Column(db.DateTime)
    closes_at = db.Column(db.DateTime)
    max_attempts = db.Column(db.Integer, nullable=False, default=1)  # 0 = unlimited
    shuffle_questions = db.Column(db.Boolean, nullable=False, default=False)
    shuffle_choices = db.Column(db.Boolean, nullable=False, default=False)
    show_results = db.Column(db.String(20), nullable=False, default="immediately")
    show_correct_answers = db.Column(db.Boolean, nullable=False, default=False)
    results_released = db.Column(db.Boolean, nullable=False, default=False)
    # The test code students type in the test portal (normalized: upper case, no separators).
    access_code = db.Column(db.String(60), nullable=False, default="")
    allow_online = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    # --- test-taking security (all enforced on the server; the browser only reports) ---
    identity_check = db.Column(db.String(20), nullable=False, default="confirm")  # see IDENTITY_CHECKS
    require_approval = db.Column(db.Boolean, nullable=False, default=False)  # proctor admits each student
    lock_to_device = db.Column(db.Boolean, nullable=False, default=True)  # one browser per attempt
    require_fullscreen = db.Column(db.Boolean, nullable=False, default=False)
    block_copy_paste = db.Column(db.Boolean, nullable=False, default=True)
    max_focus_losses = db.Column(db.Integer)  # lock the attempt after this many exits; None = only log
    allowed_networks = db.Column(db.Text, nullable=False, default="")  # IPs / CIDR ranges, one per line
    late_start_minutes = db.Column(db.Integer)  # no new starts this long after opening
    watermark = db.Column(db.Boolean, nullable=False, default=True)  # student name across the test
    # lockdown browser: "" (any browser) or "seb" (Safe Exam Browser, verified by its Config Key)
    lockdown_browser = db.Column(db.String(20), nullable=False, default="")
    seb_quit_password_hash = db.Column(db.String(64), nullable=False, default="")  # SHA-256 hex, as SEB wants it
    seb_extra_keys = db.Column(db.Text, nullable=False, default="")  # more Config Keys / Browser Exam Keys
    seb_only_listed_keys = db.Column(db.Boolean, nullable=False, default=False)  # don't accept the generated key
    # password to start: "" (none), "static" (teacher's word) or "rotating" (changes every few minutes)
    access_password_mode = db.Column(db.String(20), nullable=False, default="")
    access_password = db.Column(db.String(60), nullable=False, default="")
    password_secret = db.Column(db.String(64), nullable=False, default="")
    password_rotation_minutes = db.Column(db.Integer, nullable=False, default=5)
    bypass_password_hash = db.Column(db.String(256), nullable=False, default="")  # proctor override of the PIN
    one_sitting = db.Column(db.Boolean, nullable=False, default=False)  # leaving pauses the test
    sequential = db.Column(db.Boolean, nullable=False, default=False)  # one question at a time, no going back
    focus_grace_seconds = db.Column(db.Integer, nullable=False, default=0)  # absences shorter than this don't count
    clock_stops_when_paused = db.Column(db.Boolean, nullable=False, default=False)

    course = db.relationship("Course", back_populates="assessments")
    owner = db.relationship("User")
    items = db.relationship(
        "AssessmentItem",
        back_populates="assessment",
        cascade="all, delete-orphan",
        order_by="AssessmentItem.position",
    )
    attempts = db.relationship("Attempt", back_populates="assessment", cascade="all, delete-orphan")

    @property
    def max_score(self):
        return sum(i.effective_points for i in self.items)

    @property
    def is_locked(self):
        """Items can't be added/removed once any student has a response on record."""
        return any(a for a in self.attempts)

    def window_state(self, now=None):
        now = now or utcnow()
        if self.status == "draft":
            return "draft"
        if self.status == "closed":
            return "closed"
        if self.opens_at and now < self.opens_at:
            return "upcoming"
        if self.closes_at and now >= self.closes_at:
            return "closed"
        return "open"

    def is_open(self, now=None):
        return self.window_state(now) == "open" and self.allow_online

    def late_start_cutoff(self):
        if self.late_start_minutes and self.opens_at:
            return self.opens_at + timedelta(minutes=self.late_start_minutes)
        return None

    @property
    def network_list(self):
        return [line.strip() for line in (self.allowed_networks or "").splitlines() if line.strip()]

    @property
    def display_code(self):
        code = self.access_code or ""
        return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code

    def results_visible(self, now=None):
        if self.results_released:
            return True
        if self.show_results == "immediately":
            return True
        if self.show_results == "after_close":
            return self.window_state(now) == "closed"
        return False


class AssessmentItem(db.Model):
    __tablename__ = "assessment_items"
    __table_args__ = (db.UniqueConstraint("assessment_id", "question_id"),)

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    points = db.Column(db.Float)  # override of question.points

    assessment = db.relationship("Assessment", back_populates="items")
    question = db.relationship("Question", back_populates="items")
    responses = db.relationship("Response", back_populates="item", cascade="all, delete-orphan")

    @property
    def effective_points(self):
        return self.points if self.points is not None else self.question.points


class Attempt(db.Model):
    __tablename__ = "attempts"

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    number = db.Column(db.Integer, nullable=False, default=1)
    source = db.Column(db.String(20), nullable=False, default="online")  # online | paper
    status = db.Column(db.String(20), nullable=False, default="in_progress")  # in_progress | submitted
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    deadline = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)
    score = db.Column(db.Float, nullable=False, default=0.0)
    max_score = db.Column(db.Float, nullable=False, default=0.0)
    needs_grading = db.Column(db.Boolean, nullable=False, default=False)
    item_order = db.Column(db.Text, nullable=False, default="[]")
    choice_order = db.Column(db.Text, nullable=False, default="{}")
    entered_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    # proctoring
    extra_minutes = db.Column(db.Integer, nullable=False, default=0)  # time added by a proctor
    locked = db.Column(db.Boolean, nullable=False, default=False)  # paused until a proctor unlocks
    lock_reason = db.Column(db.String(200), nullable=False, default="")
    focus_losses = db.Column(db.Integer, nullable=False, default=0)
    ip = db.Column(db.String(64), nullable=False, default="")
    locked_at = db.Column(db.DateTime)
    position = db.Column(db.Integer, nullable=False, default=0)  # current question when navigation is restricted

    assessment = db.relationship("Assessment", back_populates="attempts")
    user = db.relationship("User", foreign_keys=[user_id], backref=db.backref("attempts", cascade="all, delete-orphan"))
    entered_by = db.relationship("User", foreign_keys=[entered_by_id])
    responses = db.relationship("Response", back_populates="attempt", cascade="all, delete-orphan")
    events = db.relationship(
        "AttemptEvent", back_populates="attempt", cascade="all, delete-orphan", order_by="AttemptEvent.id"
    )

    @property
    def percent(self):
        return (100.0 * self.score / self.max_score) if self.max_score else 0.0

    @property
    def is_submitted(self):
        return self.status == "submitted"

    @property
    def duration_seconds(self):
        if self.source != "online" or not self.submitted_at or not self.started_at:
            return None
        return max(0, int((self.submitted_at - self.started_at).total_seconds()))

    def get_item_order(self):
        try:
            return [int(x) for x in json.loads(self.item_order or "[]")]
        except (ValueError, TypeError):
            return []

    def get_choice_order(self):
        try:
            return {int(k): [int(x) for x in v] for k, v in json.loads(self.choice_order or "{}").items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def response_map(self):
        return {r.item_id: r for r in self.responses}


class Response(db.Model):
    __tablename__ = "responses"
    __table_args__ = (db.UniqueConstraint("attempt_id", "item_id"),)

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False)
    item_id = db.Column(db.Integer, db.ForeignKey("assessment_items.id", ondelete="CASCADE"), nullable=False)
    answer = db.Column(db.Text)  # JSON
    points_awarded = db.Column(db.Float)
    is_correct = db.Column(db.Boolean)
    needs_grading = db.Column(db.Boolean, nullable=False, default=False)
    manually_graded = db.Column(db.Boolean, nullable=False, default=False)
    feedback = db.Column(db.Text, nullable=False, default="")
    graded_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    graded_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    attempt = db.relationship("Attempt", back_populates="responses")
    item = db.relationship("AssessmentItem", back_populates="responses")
    graded_by = db.relationship("User")

    def get_answer(self):
        if self.answer is None:
            return None
        try:
            return json.loads(self.answer)
        except (ValueError, TypeError):
            return None

    def set_answer(self, value):
        self.answer = None if value is None else json.dumps(value)


# --------------------------------------------------------------------------- misc


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    action = db.Column(db.String(80), nullable=False)
    detail = db.Column(db.Text, nullable=False, default="")
    ip = db.Column(db.String(64), nullable=False, default="")

    user = db.relationship("User")


# --------------------------------------------------------------------------- scanned answer sheets


class AnswerSheet(db.Model):
    """One printed page of an answer sheet. Its id is encoded on the paper, and the layout
    is frozen here so a photo is always read against exactly what was printed."""

    __tablename__ = "answer_sheets"

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))  # empty = blank sheet
    page = db.Column(db.Integer, nullable=False, default=1)
    pages = db.Column(db.Integer, nullable=False, default=1)
    layout = db.Column(db.Text, nullable=False)
    layout_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    assessment = db.relationship("Assessment", backref=db.backref("answer_sheets", cascade="all, delete-orphan"))
    user = db.relationship("User")

    def get_layout(self):
        return json.loads(self.layout)


class SheetScan(db.Model):
    """A photo of an answer sheet and what was read from it."""

    __tablename__ = "sheet_scans"

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True)
    sheet_id = db.Column(db.Integer, db.ForeignKey("answer_sheets.id", ondelete="SET NULL"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    uploaded_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    status = db.Column(db.String(20), nullable=False, default="review")  # review | ready | recorded | failed | replaced
    filename = db.Column(db.String(255), nullable=False, default="")
    error = db.Column(db.Text, nullable=False, default="")
    result = db.Column(db.Text, nullable=False, default="{}")
    image = db.deferred(db.Column(db.LargeBinary))  # straightened sheet (JPEG)
    recorded_at = db.Column(db.DateTime)

    assessment = db.relationship("Assessment", backref=db.backref("scans", cascade="all, delete-orphan", order_by="SheetScan.id"))
    sheet = db.relationship("AnswerSheet")
    user = db.relationship("User", foreign_keys=[user_id])
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_id])

    def get_result(self):
        try:
            return json.loads(self.result or "{}")
        except ValueError:
            return {}

    @property
    def flag_count(self):
        return sum(1 for r in self.get_result().get("items", {}).values() if r.get("flag"))


class ApiToken(db.Model):
    """Bearer token for the integration API. Only a SHA-256 of the secret is stored."""

    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    prefix = db.Column(db.String(16), nullable=False)  # first characters, to recognise a token
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    last_used_at = db.Column(db.DateTime)
    revoked = db.Column(db.Boolean, nullable=False, default=False)

    created_by = db.relationship("User")


# --------------------------------------------------------------------------- test portal


class TestSession(db.Model):
    """A student's sign-in to the test portal for one assessment.

    The browser holds a random token in a cookie scoped to /test; only its SHA-256 is stored.
    status: confirm (code and ID accepted, identity not yet confirmed) → waiting (needs a
    proctor) → ready (may start or resume) → ended (submitted, signed out, replaced or revoked).
    """

    __tablename__ = "test_sessions"
    __test__ = False  # not a pytest class

    id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id", ondelete="SET NULL"))
    status = db.Column(db.String(20), nullable=False, default="confirm")
    end_reason = db.Column(db.String(120), nullable=False, default="")
    ip = db.Column(db.String(64), nullable=False, default="")
    user_agent = db.Column(db.String(300), nullable=False, default="")
    pin_failures = db.Column(db.Integer, nullable=False, default=0)
    lockdown_ok = db.Column(db.Boolean, nullable=False, default=False)  # proved it runs the required lockdown browser
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)

    assessment = db.relationship("Assessment", backref=db.backref("test_sessions", cascade="all, delete-orphan"))
    user = db.relationship("User", backref=db.backref("test_sessions", cascade="all, delete-orphan"))
    attempt = db.relationship("Attempt")


EVENT_LABELS = {
    "started": "Started",
    "resumed": "Resumed",
    "submitted": "Submitted",
    "focus_lost": "Left the test page",
    "fullscreen_exit": "Left full screen",
    "fullscreen_unsupported": "Browser can't do full screen",
    "copy_blocked": "Tried to copy",
    "paste_blocked": "Tried to paste",
    "contextmenu_blocked": "Tried to open the context menu",
    "print_blocked": "Tried to print",
    "ip_change": "Network changed",
    "device_change": "Moved to another device",
    "device_blocked": "Sign-in from another device refused",
    "device_reset": "Signed out by a proctor",
    "session_hijack_blocked": "Session used from another browser: refused",
    "locked": "Paused",
    "unlocked": "Allowed to continue",
    "time_added": "Time added",
    "force_submitted": "Submitted by a proctor",
    "focus_returned": "Came back to the test",
    "lockdown_verified": "Safe Exam Browser verified",
    "lockdown_refused": "Lockdown browser check failed",
    "password_bypass": "Proctor override used",
    "left_mid_test": "Left the test before submitting",
    "next_question": "Moved to the next question",
}
EVENT_ATTENTION = {
    "focus_lost", "fullscreen_exit", "copy_blocked", "paste_blocked", "print_blocked", "ip_change",
    "device_change", "device_blocked", "session_hijack_blocked", "locked", "lockdown_refused", "left_mid_test",
}


class AttemptEvent(db.Model):
    """Integrity timeline of an attempt: starts, device and network changes, focus losses, proctor actions."""

    __tablename__ = "attempt_events"

    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    kind = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.String(300), nullable=False, default="")
    ip = db.Column(db.String(64), nullable=False, default="")

    attempt = db.relationship("Attempt", back_populates="events")


class Accommodation(db.Model):
    """Per-student exceptions for one assessment: extra time and extra attempts."""

    __tablename__ = "accommodations"
    __table_args__ = (db.UniqueConstraint("assessment_id", "user_id"),)

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    extra_minutes = db.Column(db.Integer, nullable=False, default=0)
    extra_attempts = db.Column(db.Integer, nullable=False, default=0)

    assessment = db.relationship("Assessment", backref=db.backref("accommodations", cascade="all, delete-orphan"))
    user = db.relationship("User")


class AssessmentPdf(db.Model):
    """The PDF a test was made from. The answer key is entered against its question numbers."""

    __tablename__ = "assessment_pdfs"

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, unique=True)
    filename = db.Column(db.String(255), nullable=False, default="test.pdf")
    size = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=False, default="")
    pages = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    data = db.deferred(db.Column(db.LargeBinary, nullable=False))  # the original file, for printing

    assessment = db.relationship("Assessment", backref=db.backref("pdf", uselist=False, cascade="all, delete-orphan"))


class PdfPage(db.Model):
    """One page of the PDF drawn as a JPEG. Students see these, never the PDF file itself."""

    __tablename__ = "assessment_pdf_pages"
    __table_args__ = (db.UniqueConstraint("assessment_id", "page"),)

    id = db.Column(db.Integer, primary_key=True)
    assessment_id = db.Column(db.Integer, db.ForeignKey("assessments.id", ondelete="CASCADE"), nullable=False, index=True)
    page = db.Column(db.Integer, nullable=False)
    width = db.Column(db.Integer, nullable=False, default=0)
    height = db.Column(db.Integer, nullable=False, default=0)
    data = db.deferred(db.Column(db.LargeBinary, nullable=False))

    assessment = db.relationship(
        "Assessment", backref=db.backref("pdf_pages", cascade="all, delete-orphan", order_by="PdfPage.page")
    )


class PortalFailure(db.Model):
    """Failed test-portal sign-ins, for throttling (old rows are pruned)."""

    __tablename__ = "portal_failures"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    ip = db.Column(db.String(64), nullable=False, index=True)
    scope = db.Column(db.String(120), nullable=False, default="", index=True)  # e.g. "pin:12:34"
