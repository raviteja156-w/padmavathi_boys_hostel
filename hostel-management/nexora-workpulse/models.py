"""Database models. All tables use the `nwp_` prefix to avoid clashing with existing NEXORA tables."""
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Index, text

db = SQLAlchemy()


def utcnow():
    """Naive UTC timestamp generated on the server."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model):
    __tablename__ = "nwp_users"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(254), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="member")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    # Attendance counts only from the day the member first signs in.
    first_login_at = db.Column(db.DateTime)
    # Self-rating collected on first sign-in: moderate | good | high
    skill_backend = db.Column(db.String(10))
    skill_database = db.Column(db.String(10))
    skill_frontend = db.Column(db.String(10))
    onboarded_at = db.Column(db.DateTime)
    # Founders (clients) belong to one project and only see its status page and agreement.
    # Stored in its own column name so it never clashes with a leftover "project_id" column from older apps.
    project_id = db.Column("founder_project_id", db.Integer, db.ForeignKey("nwp_founder_projects.id", ondelete="SET NULL"))
    project = db.relationship("Project", back_populates="founders", foreign_keys=[project_id])
    __table_args__ = (db.CheckConstraint("role IN ('admin','member','founder')", name="ck_nwp_users_role"),)


class Task(db.Model):
    __tablename__ = "nwp_tasks"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(10), nullable=False, default="medium")
    target_hours = db.Column(db.Float, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    deadline = db.Column(db.Date, nullable=False)
    expected_deliverable = db.Column(db.String(1000))
    instructions = db.Column(db.Text)
    work_type = db.Column(db.String(12))  # backend | database | frontend | fullstack | other
    repo_url = db.Column(db.String(300))  # optional; falls back to the team repository
    created_by = db.Column(db.Integer, db.ForeignKey("nwp_users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    assignments = db.relationship("Assignment", back_populates="task", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_nwp_tasks_deadline", "deadline"),)


class Assignment(db.Model):
    """One row per (task, team member). Progress and status are tracked per member."""
    __tablename__ = "nwp_task_assignments"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("nwp_tasks.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"), nullable=False)
    status = db.Column(db.String(15), nullable=False, default="not_started")
    progress = db.Column(db.Integer, nullable=False, default=0)
    work_notes = db.Column(db.Text)
    deliverable_url = db.Column(db.String(500))
    pr_url = db.Column(db.String(500))
    pr_merged_at = db.Column(db.DateTime)
    submitted_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    task = db.relationship("Task", back_populates="assignments")
    user = db.relationship("User")
    reviews = db.relationship("AssignmentReview", back_populates="assignment",
                              order_by="AssignmentReview.created_at.desc()", cascade="all, delete-orphan")
    updates = db.relationship("TaskUpdate", back_populates="assignment",
                              order_by="TaskUpdate.created_at.desc()", cascade="all, delete-orphan")
    __table_args__ = (
        db.UniqueConstraint("task_id", "user_id", name="uq_nwp_assignment_task_user"),
        Index("ix_nwp_assign_user_status", "user_id", "status"),
    )


class AssignmentReview(db.Model):
    __tablename__ = "nwp_task_reviews"
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("nwp_task_assignments.id", ondelete="CASCADE"), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"), nullable=False)
    decision = db.Column(db.String(10), nullable=False)  # approved | changes
    feedback = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    assignment = db.relationship("Assignment", back_populates="reviews")
    reviewer = db.relationship("User")
    __table_args__ = (Index("ix_nwp_reviews_assignment", "assignment_id"),)


class TaskUpdate(db.Model):
    """Daily progress update a member posts on one of their tasks (optionally with a pull request link)."""
    __tablename__ = "nwp_task_updates"
    id = db.Column(db.Integer, primary_key=True)
    assignment_id = db.Column(db.Integer, db.ForeignKey("nwp_task_assignments.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"), nullable=False)
    update_date = db.Column(db.Date, nullable=False)  # local calendar day, server-derived
    progress = db.Column(db.Integer, nullable=False, default=0)
    note = db.Column(db.Text, nullable=False)
    pr_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    assignment = db.relationship("Assignment", back_populates="updates")
    user = db.relationship("User")
    __table_args__ = (Index("ix_nwp_updates_user_date", "user_id", "update_date"),
                      Index("ix_nwp_updates_assignment", "assignment_id"))


class Setting(db.Model):
    """Small key/value store for portal settings (e.g. the team GitHub repository)."""
    __tablename__ = "nwp_settings"
    key = db.Column(db.String(60), primary_key=True)
    value = db.Column(db.String(1000))
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class AttendanceSession(db.Model):
    __tablename__ = "nwp_attendance_sessions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"), nullable=False)
    clock_in = db.Column(db.DateTime, nullable=False, default=utcnow)
    clock_out = db.Column(db.DateTime)
    incomplete = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    user = db.relationship("User")
    breaks = db.relationship("BreakRecord", back_populates="session", lazy="selectin",
                             cascade="all, delete-orphan", order_by="BreakRecord.started_at")
    __table_args__ = (
        Index("ix_nwp_att_user_in", "user_id", "clock_in"),
        # Database-level guarantee: at most one open session per user.
        Index("uq_nwp_att_one_open", "user_id", unique=True,
              postgresql_where=text("clock_out IS NULL"), sqlite_where=text("clock_out IS NULL")),
    )


class BreakRecord(db.Model):
    __tablename__ = "nwp_break_records"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("nwp_attendance_sessions.id", ondelete="CASCADE"), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    ended_at = db.Column(db.DateTime)
    session = db.relationship("AttendanceSession", back_populates="breaks")
    __table_args__ = (
        Index("ix_nwp_break_session", "session_id"),
        Index("uq_nwp_break_one_open", "session_id", unique=True,
              postgresql_where=text("ended_at IS NULL"), sqlite_where=text("ended_at IS NULL")),
    )


class Acknowledgement(db.Model):
    __tablename__ = "nwp_acknowledgements"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"), nullable=False)
    ack_date = db.Column(db.Date, nullable=False)  # local calendar day, server-derived
    tasks_worked = db.Column(db.Text, nullable=False)
    work_completed = db.Column(db.Text, nullable=False)
    deliverable_links = db.Column(db.Text)
    problems = db.Column(db.Text)
    pending_work = db.Column(db.Text)
    next_steps = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    user = db.relationship("User")
    __table_args__ = (Index("ix_nwp_ack_user_date", "user_id", "ack_date"),)


class AuditLog(db.Model):
    __tablename__ = "nwp_audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"))
    action = db.Column(db.String(60), nullable=False)
    detail = db.Column(db.String(500))
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    user = db.relationship("User")
    __table_args__ = (Index("ix_nwp_audit_created", "created_at"),)


# --------------------------------------------------------------------------- founder projects
class Project(db.Model):
    """A client/founder project. Everything here is controlled by admins and shown on the founder's page."""
    __tablename__ = "nwp_founder_projects"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    summary = db.Column(db.Text)
    live_url = db.Column(db.String(500))
    phase = db.Column(db.String(20), nullable=False, default="discovery")
    status = db.Column(db.String(20), nullable=False, default="on_track")
    progress = db.Column(db.Integer, nullable=False, default=0)
    status_note = db.Column(db.Text)
    start_date = db.Column(db.Date)
    target_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    founders = db.relationship("User", back_populates="project", foreign_keys="User.project_id")
    milestones = db.relationship("Milestone", back_populates="project", cascade="all, delete-orphan",
                                 order_by="(Milestone.sort_order, Milestone.id)")
    updates = db.relationship("ProjectUpdate", back_populates="project", cascade="all, delete-orphan",
                              order_by="ProjectUpdate.created_at.desc()")
    requests = db.relationship("ChangeRequest", back_populates="project", cascade="all, delete-orphan",
                               order_by="ChangeRequest.created_at.desc()")
    agreement = db.relationship("Agreement", back_populates="project", uselist=False, cascade="all, delete-orphan")


class Milestone(db.Model):
    __tablename__ = "nwp_founder_milestones"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("nwp_founder_projects.id", ondelete="CASCADE"), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    due_date = db.Column(db.Date)
    status = db.Column(db.String(15), nullable=False, default="pending")  # pending | in_progress | done
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    project = db.relationship("Project", back_populates="milestones")
    __table_args__ = (Index("ix_nwp_fmilestones_project", "project_id"),)


class ProjectUpdate(db.Model):
    """A news/status post from the admin that founders read on their page."""
    __tablename__ = "nwp_founder_updates"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("nwp_founder_projects.id", ondelete="CASCADE"), nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"))
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    project = db.relationship("Project", back_populates="updates")
    author = db.relationship("User")
    __table_args__ = (Index("ix_nwp_fupdates_project", "project_id"),)


class ChangeRequest(db.Model):
    """A change the founder asks for after viewing the live application."""
    __tablename__ = "nwp_founder_requests"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("nwp_founder_projects.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"))
    title = db.Column(db.String(150), nullable=False)
    details = db.Column(db.Text, nullable=False)
    priority = db.Column(db.String(10), nullable=False, default="normal")
    page_url = db.Column(db.String(500))
    status = db.Column(db.String(15), nullable=False, default="open")  # open | in_progress | done | declined
    admin_reply = db.Column(db.Text)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    project = db.relationship("Project", back_populates="requests")
    user = db.relationship("User")
    __table_args__ = (Index("ix_nwp_frequests_project", "project_id", "status"),)


class Agreement(db.Model):
    """Agreement text written by the admin. Every edit raises the version, so founders must sign again."""
    __tablename__ = "nwp_founder_agreements"
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("nwp_founder_projects.id", ondelete="CASCADE"), nullable=False, unique=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_by = db.Column(db.Integer, db.ForeignKey("nwp_users.id"))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    project = db.relationship("Project", back_populates="agreement")
    signatures = db.relationship("AgreementSignature", back_populates="agreement", cascade="all, delete-orphan",
                                 order_by="AgreementSignature.signed_at.desc()")


class AgreementSignature(db.Model):
    """Digital approval: typed legal name + consent, with time, IP and a fingerprint of the exact text signed."""
    __tablename__ = "nwp_founder_signatures"
    id = db.Column(db.Integer, primary_key=True)
    agreement_id = db.Column(db.Integer, db.ForeignKey("nwp_founder_agreements.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("nwp_users.id"))
    version = db.Column(db.Integer, nullable=False)
    signed_name = db.Column(db.String(120), nullable=False)
    body_hash = db.Column(db.String(64), nullable=False)
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))
    signed_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    agreement = db.relationship("Agreement", back_populates="signatures")
    user = db.relationship("User")
    __table_args__ = (Index("ix_nwp_fsignatures_agreement", "agreement_id"),)
