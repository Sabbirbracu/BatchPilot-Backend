import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Time,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, SoftDeleteMixin, TimestampMixin

WEEKDAYS = ("sat", "sun", "mon", "tue", "wed", "thu", "fri")
FEE_PLANS = ("monthly", "course")  # course = one fee for the whole course, paid at once or in instalments


class Batch(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "batches"

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(120))
    class_level: Mapped[str | None] = mapped_column(String(60))
    days: Mapped[list[str]] = mapped_column(ARRAY(String(3)), default=list, nullable=False)
    start_time: Mapped[time | None] = mapped_column(Time)
    end_time: Mapped[time | None] = mapped_column(Time)
    monthly_fee: Mapped[int] = mapped_column(BigInteger, nullable=False)  # poisha
    admission_fee: Mapped[int | None] = mapped_column(BigInteger)  # poisha
    capacity: Mapped[int | None] = mapped_column(Integer)
    fee_plan: Mapped[str] = mapped_column(String(10), default="monthly", server_default="monthly", nullable=False)
    course_fee: Mapped[int | None] = mapped_column(BigInteger)  # poisha; fee_plan == "course"
    installments: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1", nullable=False)
    location: Mapped[str | None] = mapped_column(String(160))  # shown on admission pages
    starts_on: Mapped[date | None] = mapped_column(Date)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Student(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "students"
    __table_args__ = (
        Index("ix_students_inst_status", "institution_id", "status"),
        Index("ix_students_inst_guardian_phone", "institution_id", "guardian_phone"),
        Index(
            "uq_students_inst_code",
            "institution_id",
            "student_code",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_students_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    student_code: Mapped[str] = mapped_column(String(30), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20))
    guardian_name: Mapped[str | None] = mapped_column(String(120))
    guardian_relation: Mapped[str | None] = mapped_column(String(10))  # father | mother | other
    guardian_phone: Mapped[str | None] = mapped_column(String(20))
    guardian_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    school: Mapped[str | None] = mapped_column(String(160))
    class_level: Mapped[str | None] = mapped_column(String(60))
    photo_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(10), default="active", nullable=False)  # active | left
    joined_at: Mapped[date] = mapped_column(Date, nullable=False)
    left_at: Mapped[date | None] = mapped_column(Date)
    qr_token: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Enrollment(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A student in a batch. One student can be in several batches, each with its own fee."""

    __tablename__ = "enrollments"
    __table_args__ = (
        Index("ix_enrollments_batch_open", "batch_id", postgresql_where=text("end_month IS NULL")),
        Index(
            "uq_enrollments_open",
            "student_id",
            "batch_id",
            unique=True,
            postgresql_where=text("end_month IS NULL AND deleted_at IS NULL"),
        ),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), index=True, nullable=False
    )
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id"), index=True, nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), nullable=False)
    custom_fee: Mapped[int | None] = mapped_column(BigInteger)  # poisha; overrides batch fee
    discount_note: Mapped[str | None] = mapped_column(String(200))
    start_month: Mapped[date] = mapped_column(Date, nullable=False)
    end_month: Mapped[date | None] = mapped_column(Date)
