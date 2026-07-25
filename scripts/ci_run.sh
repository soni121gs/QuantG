#!/usr/bin/env bash
set -euo pipefail

LOG_PATH="${QUANTG_CI_LOG:-/var/log/quantg_ci.log}"
APP_DIR="${QUANTG_APP_DIR:-/opt/QuantG}"
SERVICE="${QUANTG_BACKEND_SERVICE:-quantg-backend}"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "==== QuantG CI run ${STAMP} ===="
  cd "${APP_DIR}"

  echo "[1/4] offline pytest"
  docker exec "${SERVICE}" bash -lc 'cd /app/backend && python -m pytest tests/ -m "not integration" --ignore=tests/test_live_readiness.py --ignore=tests/test_execution_bridge_upstox_only.py'

  echo "[2/4] diagnostics"
  docker exec "${SERVICE}" bash -lc 'cd /app && python - <<'"'"'PY'"'"'
import asyncio
from core import db
from core.hermes_diagnostics import run_diagnostics

async def main():
    users = await db.users.find({}, {"_id": 0, "id": 1}).to_list(1000)
    for row in users:
        uid = row.get("id")
        if uid:
            print(await run_diagnostics(db, uid, persist=True, auto_oos=False))

asyncio.run(main())
PY'

  echo "[3/4] Edge Lab snapshot"
  docker exec "${SERVICE}" bash -lc 'cd /app && python /app/scripts/build_edge_lab_snapshot.py'

  echo "[4/4] RAG reindex"
  docker exec "${SERVICE}" bash -lc 'cd /app && python /app/scripts/research_rag_reindex.py'

  echo "==== QuantG CI completed $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="
} >>"${LOG_PATH}" 2>&1
