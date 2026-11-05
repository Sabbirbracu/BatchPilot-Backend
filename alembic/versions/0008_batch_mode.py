"""Batches get a mode: offline (in a room) or online. Existing batches are offline.

Revision ID: 0008
Revises: 0007
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("batches", sa.Column("mode", sa.String(10), server_default="offline", nullable=False))


def downgrade() -> None:
    op.drop_column("batches", "mode")
