import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, SoftDeleteMixin, TimestampMixin

ROLES = ("owner", "assistant", "teacher")
PORTALS = ("teacher", "staff", "guardian", "student")
# The institution owner is the "teacher" (স্যার); assistants and hired teachers use the staff portal.
PORTAL_BY_ROLE = {"owner": "teacher", "assistant": "staff", "teacher": "staff"}

DEFAULT_SETTINGS: dict = {
    "receipt_sms": True,
    "absent_sms": True,
    "reminders_enabled": True,
    "daily_summary": True,
    "summary_time": "21:00",
    "bkash_number": None,
}


class User(IdMixin, TimestampMixin, Base):
    """Global person record. A user can belong to more than one institution."""

    __tablename__ = "users"

    phone: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    language: Mapped[str] = mapped_column(String(5), default="bn", nullable=False)
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class OtpRequest(IdMixin, Base):
    __tablename__ = "otp_requests"

    phone: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Session(IdMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    active_institution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # Which dashboard this session uses: teacher | staff | guardian | student (None = not set up yet).
    portal: Mapped[str | None] = mapped_column(String(10))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Institution(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "institutions"

    name: Mapped[str] = mapped_column(String(160), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80))
    slug: Mapped[str | None] = mapped_column(String(63), unique=True)
    logo_url: Mapped[str | None] = mapped_column(String(500))
    fee_due_day: Mapped[int] = mapped_column(SmallInteger, default=10, nullable=False)
    timezone: Mapped[str] = mapped_column(String(40), default="Asia/Dhaka", nullable=False)
    plan: Mapped[str] = mapped_column(String(20), default="pro", nullable=False)
    plan_status: Mapped[str] = mapped_column(String(20), default="beta", nullable=False)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_student_code: Mapped[int] = mapped_column(Integer, default=100001, nullable=False)  # six-digit student IDs
    settings: Mapped[dict] = mapped_column(JSONB, default=lambda: dict(DEFAULT_SETTINGS), nullable=False)


class Membership(IdMixin, TimestampMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "institution_id"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
