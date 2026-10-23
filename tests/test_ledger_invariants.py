"""System Design §8 invariants: must return zero rows across the whole database."""

from sqlalchemy import create_engine, text

from app.core.config import get_settings

INVARIANTS = {
    "due_paid_equals_allocations": """
        SELECT d.id FROM fee_dues d LEFT JOIN payment_allocations a ON a.fee_due_id = d.id
        GROUP BY d.id HAVING d.paid_amount <> COALESCE(SUM(a.amount),0)""",
    "payment_fully_allocated": """
        SELECT p.id FROM payments p LEFT JOIN payment_allocations a ON a.payment_id = p.id
        GROUP BY p.id HAVING p.amount <> COALESCE(SUM(a.amount),0)""",
    "no_overpaid_due": "SELECT id FROM fee_dues WHERE paid_amount > amount - discount",
    "rls_on_every_tenant_table": """
        SELECT c.relname FROM pg_class c JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE a.attname = 'institution_id' AND c.relkind = 'r'
          AND c.relnamespace = 'public'::regnamespace AND NOT c.relrowsecurity""",
}


def test_invariants_hold():
    # Cross-tenant check, so it runs as bp_ops (BYPASSRLS) like the nightly reconciliation job will.
    eng = create_engine(get_settings().ops_database_url)
    with eng.connect() as conn:
        for name, sql in INVARIANTS.items():
            rows = conn.execute(text(sql)).all()
            assert rows == [], f"{name}: {rows[:5]}"
