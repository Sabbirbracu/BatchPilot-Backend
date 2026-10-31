"""Students remember how the guardian is related (বাবা / মা / অন্য), asked on the admission form.

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("students", sa.Column("guardian_relation", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("students", "guardian_relation")
