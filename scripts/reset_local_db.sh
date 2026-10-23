#!/usr/bin/env bash
# LOCAL ONLY: drops the database and rebuilds it with demo data.
set -euo pipefail
cd "$(dirname "$0")/.."
DB=${DB:-batchpilot}
psql -d postgres -q -c "DROP DATABASE IF EXISTS $DB WITH (FORCE);"
./scripts/setup_local_db.sh
.venv/bin/python -m scripts.seed_demo
