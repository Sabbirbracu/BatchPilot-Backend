"""Admissions: teacher-made QR links (one per course, listing its batches) and the join requests
families send from them. A request is not a student yet; it becomes one when the teacher admits it
(or straight away when it was paid online)."""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, SmallInteger, String, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, SoftDeleteMixin, TimestampMixin

OFFER_KINDS = ("percent", "amount")
OFFER_TARGETS = ("admission", "fee")  # fee = first month (monthly batches) or the course fee
REQUEST_STATUSES = ("pending", "admitted", "rejected", "expired", "cancelled")
REQUEST_PAY_METHODS = ("cash", "bkash", "nagad")


class AdmissionLink(IdMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "admission_links"

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))  # free text shown on the page
    code: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)  # public: /join/<code>
    batch_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), default=list, nullable=False)
    offer_kind: Mapped[str | None] = mapped_column(String(10))
    offer_value: Mapped[int | None] = mapped_column(BigInteger)  # percent (1-100) or poisha
    offer_on: Mapped[str] = mapped_column(String(10), default="admission", nullable=False)
    offer_until: Mapped[date | None] = mapped_column(Date)  # last day the offer applies
    hold_days: Mapped[int] = mapped_column(SmallInteger, default=3, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    scan_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)


class AdmissionRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "admission_requests"
    __table_args__ = (
        Index("ix_admission_requests_open", "institution_id", "batch_id", postgresql_where=text("status = 'pending'")),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    link_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("admission_links.id"), index=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)  # who applied
    student_name: Mapped[str] = mapped_column(String(120), nullable=False)
    student_phone: Mapped[str | None] = mapped_column(String(20))
    guardian_name: Mapped[str | None] = mapped_column(String(120))
    guardian_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    school: Mapped[str | None] = mapped_column(String(160))
    class_level: Mapped[str | None] = mapped_column(String(60))
    pay_method: Mapped[str] = mapped_column(String(10), default="cash", nullable=False)
    trx_id: Mapped[str | None] = mapped_column(String(60))
    # Offer frozen at the time of applying, so a later deadline doesn't take it away.
    offer_discount_admission: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    offer_discount_fee: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="pending", nullable=False)
    hold_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    student_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("students.id"))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(String(200))
