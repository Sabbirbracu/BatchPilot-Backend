import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin

OUTBOX_STATUSES = ("queued", "held", "sending", "sent", "delivered", "failed", "cancelled")


class MessageTemplate(IdMixin, TimestampMixin, Base):
    __tablename__ = "message_templates"
    __table_args__ = (UniqueConstraint("institution_id", "key"),)

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(30), nullable=False)
    body_bn: Mapped[str] = mapped_column(Text, nullable=False)
    body_en: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class OutboxMessage(IdMixin, TimestampMixin, Base):
    """Written in the same transaction as the business change; a worker sends it later."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index(
            "ix_outbox_pending",
            "status",
            "send_after",
            postgresql_where=text("status IN ('queued','held')"),
        ),
        Index("ix_outbox_inst_created", "institution_id", text("created_at DESC")),
    )

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    student_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("students.id"), index=True)
    to_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    template_key: Mapped[str] = mapped_column(String(30), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    segments: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, default=5, nullable=False)  # receipts = 1
    status: Mapped[str] = mapped_column(String(12), default="queued", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    send_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str | None] = mapped_column(String(30))
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    last_error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
