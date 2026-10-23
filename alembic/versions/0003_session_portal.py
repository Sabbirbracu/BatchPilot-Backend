"""Sessions remember which portal (dashboard) they belong to.

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("portal", sa.String(length=10), nullable=True))
    # Existing sessions: owners -> teacher, everyone else with a membership -> staff.
    op.execute(
        """
        UPDATE sessions s SET portal = CASE m.role WHEN 'owner' THEN 'teacher' ELSE 'staff' END
        FROM memberships m
        WHERE m.user_id = s.user_id AND m.institution_id = s.active_institution_id
        """
    )


def downgrade() -> None:
    op.drop_column("sessions", "portal")
