#!/usr/bin/env bash
# Creates the local database and the three roles, then runs migrations.
#   bp_owner  owns the tables, runs migrations
#   bp_app    the API (NOBYPASSRLS: Row-Level Security always applies)
#   bp_ops    cross-tenant jobs only (BYPASSRLS, no DELETE)
# Needs a Postgres superuser (Homebrew's default user is fine).
set -euo pipefail
cd "$(dirname "$0")/.."
DB=${DB:-batchpilot}
export MIGRATION_DATABASE_URL="postgresql+psycopg://bp_owner:bp_owner_dev@localhost:5432/$DB"

psql -d postgres -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bp_owner') THEN CREATE ROLE bp_owner LOGIN PASSWORD 'bp_owner_dev'; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bp_app') THEN CREATE ROLE bp_app LOGIN PASSWORD 'bp_app_dev' NOBYPASSRLS; END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='bp_ops') THEN CREATE ROLE bp_ops LOGIN PASSWORD 'bp_ops_dev' BYPASSRLS; END IF;
END \$\$;
SELECT 'CREATE DATABASE $DB OWNER bp_owner' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='$DB')\gexec
SQL
psql -d "$DB" -v ON_ERROR_STOP=1 -q -c "CREATE EXTENSION IF NOT EXISTS pg_trgm; ALTER SCHEMA public OWNER TO bp_owner; GRANT CONNECT ON DATABASE $DB TO bp_app, bp_ops;"
psql -d "$DB" -U bp_owner -v ON_ERROR_STOP=1 -q -c "
  GRANT USAGE ON SCHEMA public TO bp_app, bp_ops;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bp_app;
  ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO bp_ops;"
.venv/bin/alembic upgrade head
psql -d "$DB" -U bp_owner -q -c "GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO bp_ops;"
echo "Database $DB ready."
