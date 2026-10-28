"""Named notice templates: owners keep their own notice texts next to the automatic ones.

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("message_templates", sa.Column("name", sa.String(length=60), nullable=True))


def downgrade() -> None:
    op.drop_column("message_templates", "name")
