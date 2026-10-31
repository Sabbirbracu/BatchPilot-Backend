"""Roll numbers become six-digit student IDs (100001, …) that RFID cards and fingerprint machines can use.

Existing students are renumbered per institution in joining order, and SMS templates that
said "রোল {roll}" now say "আইডি {student_id}".

Revision ID: 0007
Revises: 0006
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# The owner role is subject to RLS too (FORCE), so the data fixes run with it lifted.
TABLES = ("students", "institutions", "message_templates")


def upgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")

    op.alter_column("students", "roll_no", new_column_name="student_code")
    op.execute("ALTER INDEX uq_students_inst_roll RENAME TO uq_students_inst_code")
    # Two passes so a new ID never collides with an old roll still in the unique index.
    op.execute(
        """
        UPDATE students s SET student_code = '-' || x.n
        FROM (SELECT id, row_number() OVER (PARTITION BY institution_id ORDER BY joined_at, created_at, id) AS n FROM students) x
        WHERE s.id = x.id
        """
    )
    op.execute("UPDATE students SET student_code = (100000 - student_code::int)::text")

    op.alter_column("institutions", "next_roll", new_column_name="next_student_code")
    op.execute(
        "UPDATE institutions i SET next_student_code = 100001 + (SELECT count(*) FROM students s WHERE s.institution_id = i.id)"
    )

    for col in ("body_bn", "body_en"):
        op.execute(
            f"""
            UPDATE message_templates SET {col} =
              replace(replace(replace({col}, 'রোল {{roll}}', 'আইডি {{student_id}}'), 'roll {{roll}}', 'ID {{student_id}}'), '{{roll}}', '{{student_id}}')
            WHERE {col} LIKE '%{{roll}}%'
            """
        )

    for t in TABLES:
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
    for col in ("body_bn", "body_en"):
        op.execute(
            f"UPDATE message_templates SET {col} = replace(replace({col}, 'আইডি {{student_id}}', 'রোল {{roll}}'), '{{student_id}}', '{{roll}}') "
            f"WHERE {col} LIKE '%{{student_id}}%'"
        )
    op.alter_column("institutions", "next_student_code", new_column_name="next_roll")
    op.execute("ALTER INDEX uq_students_inst_code RENAME TO uq_students_inst_roll")
    op.alter_column("students", "student_code", new_column_name="roll_no")
    for t in TABLES:
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
