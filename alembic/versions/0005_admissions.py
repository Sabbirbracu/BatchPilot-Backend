"""Admissions: batch fee plans (monthly or course fee), one-time dues (admission, course, instalments),
teacher-made admission QR links and the join requests families send from them.

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INST = "NULLIF(current_setting('app.institution_id', true), '')::uuid"
NEW_TABLES = ["admission_links", "admission_requests"]


def upgrade() -> None:
    op.add_column("batches", sa.Column("fee_plan", sa.String(length=10), server_default="monthly", nullable=False))
    op.add_column("batches", sa.Column("course_fee", sa.BigInteger(), nullable=True))
    op.add_column("batches", sa.Column("installments", sa.SmallInteger(), server_default="1", nullable=False))
    op.add_column("batches", sa.Column("location", sa.String(length=160), nullable=True))
    op.add_column("batches", sa.Column("starts_on", sa.Date(), nullable=True))

    op.add_column("fee_dues", sa.Column("kind", sa.String(length=12), server_default="monthly", nullable=False))
    op.add_column("fee_dues", sa.Column("label", sa.String(length=60), nullable=True))
    op.drop_constraint("uq_fee_dues_enrollment_id_month", "fee_dues", type_="unique")
    op.create_index(
        "uq_fee_dues_monthly", "fee_dues", ["enrollment_id", "month"], unique=True,
        postgresql_where=sa.text("kind = 'monthly'"),
    )
    op.create_index(
        "uq_fee_dues_other", "fee_dues", ["enrollment_id", "kind", "month"], unique=True,
        postgresql_where=sa.text("kind <> 'monthly'"),
    )

    op.create_table(
        "admission_links",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("institution_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column("batch_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("offer_kind", sa.String(length=10), nullable=True),
        sa.Column("offer_value", sa.BigInteger(), nullable=True),
        sa.Column("offer_on", sa.String(length=10), nullable=False),
        sa.Column("offer_until", sa.Date(), nullable=True),
        sa.Column("hold_days", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("scan_count", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["institution_id"], ["institutions.id"], name=op.f("fk_admission_links_institution_id_institutions")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name=op.f("fk_admission_links_created_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admission_links")),
        sa.UniqueConstraint("code", name=op.f("uq_admission_links_code")),
    )
    op.create_index(op.f("ix_admission_links_institution_id"), "admission_links", ["institution_id"])

    op.create_table(
        "admission_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("institution_id", sa.UUID(), nullable=False),
        sa.Column("link_id", sa.UUID(), nullable=True),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("student_name", sa.String(length=120), nullable=False),
        sa.Column("student_phone", sa.String(length=20), nullable=True),
        sa.Column("guardian_name", sa.String(length=120), nullable=True),
        sa.Column("guardian_phone", sa.String(length=20), nullable=False),
        sa.Column("school", sa.String(length=160), nullable=True),
        sa.Column("class_level", sa.String(length=60), nullable=True),
        sa.Column("pay_method", sa.String(length=10), nullable=False),
        sa.Column("trx_id", sa.String(length=60), nullable=True),
        sa.Column("offer_discount_admission", sa.BigInteger(), nullable=False),
        sa.Column("offer_discount_fee", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("hold_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("student_id", sa.UUID(), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["institution_id"], ["institutions.id"], name=op.f("fk_admission_requests_institution_id_institutions")),
        sa.ForeignKeyConstraint(["link_id"], ["admission_links.id"], name=op.f("fk_admission_requests_link_id_admission_links")),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], name=op.f("fk_admission_requests_batch_id_batches")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_admission_requests_user_id_users")),
        sa.ForeignKeyConstraint(["student_id"], ["students.id"], name=op.f("fk_admission_requests_student_id_students")),
        sa.ForeignKeyConstraint(["decided_by"], ["users.id"], name=op.f("fk_admission_requests_decided_by_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admission_requests")),
    )
    op.create_index(op.f("ix_admission_requests_link_id"), "admission_requests", ["link_id"])
    op.create_index(op.f("ix_admission_requests_user_id"), "admission_requests", ["user_id"])
    op.create_index(
        "ix_admission_requests_open", "admission_requests", ["institution_id", "batch_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    for t in NEW_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} USING (institution_id = {INST}) "
            f"WITH CHECK (institution_id = {INST})"
        )
        # Links are soft-deleted and requests are history, so neither role deletes rows.
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {t} TO bp_app, bp_ops")
        op.execute(f"REVOKE DELETE ON {t} FROM bp_app")


def downgrade() -> None:
    op.drop_table("admission_requests")
    op.drop_table("admission_links")
    op.drop_index("uq_fee_dues_other", table_name="fee_dues")
    op.drop_index("uq_fee_dues_monthly", table_name="fee_dues")
    op.execute("DELETE FROM fee_dues WHERE kind <> 'monthly'")
    op.create_unique_constraint("uq_fee_dues_enrollment_id_month", "fee_dues", ["enrollment_id", "month"])
    op.drop_column("fee_dues", "label")
    op.drop_column("fee_dues", "kind")
    for c in ("starts_on", "location", "installments", "course_fee", "fee_plan"):
        op.drop_column("batches", c)
