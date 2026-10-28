#!/bin/sh
# Production migration job (run as a one-off container, see docker-compose.yml `migrate`).
#   1. alembic upgrade head      runs as bp_owner (MIGRATION_DATABASE_URL)
#   2. grant bp_ops its access   to the tables that now exist (cross-tenant jobs only)
# The database, the three roles and their default privileges are created once by the
# database container's init script; this script only moves the SCHEMA forward.
set -eu
cd "$(dirname "$0")/.."

alembic upgrade head

python - <<'PY'
from sqlalchemy import create_engine, text

from app.core.config import get_settings

engine = create_engine(get_settings().migration_database_url)
with engine.begin() as conn:
    conn.execute(text("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bp_ops"))
print("bp_ops grants refreshed")
PY
