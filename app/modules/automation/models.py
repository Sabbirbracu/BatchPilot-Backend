import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, IdMixin, TimestampMixin


class AutomationRun(IdMixin, TimestampMixin, Base):
    """Makes scheduled jobs safe to re-run: one row per (type, institution, period)."""

    __tablename__ = "automation_runs"
    __table_args__ = (UniqueConstraint("type", "institution_id", "period_key"),)

    institution_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("institutions.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    period_key: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    stats: Mapped[dict | None] = mapped_column(JSONB)
