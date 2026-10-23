import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin

ATTENDANCE_STATUSES = ("present", "absent", "late")


class AttendanceSession(IdMixin, TimestampMixin, Base):
    __tablename__ = "attendance_sessions"
    __table_args__ = (UniqueConstraint("batch_id", "date"),)

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), index=True, nullable=False
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    method: Mapped[str] = mapped_column(String(12), default="tap", nullable=False)  # tap | qr | fingerprint
    taken_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)


class AttendanceRecord(IdMixin, TimestampMixin, Base):
    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id"),
        Index("ix_attendance_records_student_session", "student_id", "session_id"),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), index=True, nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("attendance_sessions.id"), nullable=False)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id"), nullable=False)
    # Kept on each row so the table can be range-partitioned by month later.
    date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
