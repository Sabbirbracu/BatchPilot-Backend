"""Fee ledger. Money is bigint poisha. Balances are derived from fee_dues, never stored freely.
Payments are append-only: mistakes are fixed with a reversal row, never an edit or delete."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin

PAYMENT_METHODS = ("cash", "bkash", "nagad", "bank")
DUE_STATUSES = ("due", "partial", "paid", "waived")
# monthly: one per enrollment per month. admission: one-time on joining. course / installment: course-fee batches.
DUE_KINDS = ("monthly", "admission", "course", "installment")


class FeeDue(IdMixin, TimestampMixin, Base):
    __tablename__ = "fee_dues"
    __table_args__ = (
        # One monthly due per enrollment per month; other kinds are keyed by (kind, month) as well.
        Index("uq_fee_dues_monthly", "enrollment_id", "month", unique=True, postgresql_where=text("kind = 'monthly'")),
        Index(
            "uq_fee_dues_other", "enrollment_id", "kind", "month", unique=True, postgresql_where=text("kind <> 'monthly'")
        ),
        Index("ix_fee_dues_student_month", "student_id", "month"),
        Index(
            "ix_fee_dues_open",
            "institution_id",
            "status",
            "due_date",
            postgresql_where=text("status IN ('due','partial')"),
        ),
        CheckConstraint("paid_amount >= 0", name="paid_non_negative"),
        CheckConstraint("discount >= 0 AND discount <= amount", name="discount_range"),
        CheckConstraint("paid_amount <= amount - discount", name="not_overpaid"),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id"), nullable=False)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("enrollments.id"), nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), index=True, nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)  # first day of month
    kind: Mapped[str] = mapped_column(String(12), default="monthly", server_default="monthly", nullable=False)
    label: Mapped[str | None] = mapped_column(String(60))  # shown instead of the month for non-monthly dues
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    discount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    discount_reason: Mapped[str | None] = mapped_column(String(200))
    paid_amount: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(10), default="due", nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)


class Payment(IdMixin, TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_inst_received", "institution_id", text("received_at DESC")),
        Index("ix_payments_receiver_handover", "received_by", "handover_id"),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    student_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("students.id"), index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), default="payment", nullable=False)  # payment | reversal
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # negative for a reversal
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    trx_id: Mapped[str | None] = mapped_column(String(60))
    note: Mapped[str | None] = mapped_column(String(300))
    received_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    handover_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cash_handovers.id"))
    reverses_payment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("payments.id"), unique=True
    )
    reversal_reason: Mapped[str | None] = mapped_column(String(300))


class PaymentAllocation(IdMixin, Base):
    __tablename__ = "payment_allocations"

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True, nullable=False)
    fee_due_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fee_dues.id"), index=True, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)


class CashHandover(IdMixin, TimestampMixin, Base):
    __tablename__ = "cash_handovers"

    institution_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("institutions.id"), index=True, nullable=False
    )
    assistant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    handed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_amount: Mapped[int | None] = mapped_column(BigInteger)
    difference: Mapped[int | None] = mapped_column(BigInteger)

