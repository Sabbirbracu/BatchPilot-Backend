"""Row-Level Security on every tenant table + least-privilege grants for bp_app.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# NULLIF guards against '' (what current_setting returns after a SET LOCAL has ended).
INST = "NULLIF(current_setting('app.institution_id', true), '')::uuid"
USER = "NULLIF(current_setting('app.user_id', true), '')::uuid"

TENANT_TABLES = [
    "batches",
    "students",
    "enrollments",
    "fee_dues",
    "payments",
    "payment_allocations",
    "cash_handovers",
    "automation_runs",
    "attendance_sessions",
    "attendance_records",
    "message_templates",
    "outbox_messages",
    "audit_logs",
]

# Money and history are append-only for the app role.
NO_DELETE = ["payments", "payment_allocations", "audit_logs"]


def upgrade() -> None:
    for t in TENANT_TABLES:
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} USING (institution_id = {INST}) "
            f"WITH CHECK (institution_id = {INST})"
        )

    # A user can always see their own memberships (to pick an institution after login).
    op.execute("ALTER TABLE memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memberships FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON memberships "
        f"USING (institution_id = {INST} OR user_id = {USER}) "
        f"WITH CHECK (institution_id = {INST})"
    )

    op.execute("ALTER TABLE institutions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE institutions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON institutions "
        f"USING (id = {INST} OR id IN (SELECT institution_id FROM memberships WHERE user_id = {USER})) "
        f"WITH CHECK (id = {INST})"
    )

    op.execute("GRANT USAGE ON SCHEMA public TO bp_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO bp_app")
    op.execute("REVOKE ALL ON alembic_version FROM bp_app")
    for t in NO_DELETE:
        op.execute(f"REVOKE DELETE ON {t} FROM bp_app")
    op.execute("REVOKE UPDATE ON audit_logs FROM bp_app")


def downgrade() -> None:
    for t in TENANT_TABLES + ["memberships", "institutions"]:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM bp_app")
