#!/usr/bin/env bash
set -euo pipefail

LOG_PATH="${QUANTG_CI_LOG:-/var/log/quantg_ci.log}"
APP_DIR="${QUANTG_APP_DIR:-/opt/QuantG}"
SERVICE="${QUANTG_BACKEND_SERVICE:-quantg-backend}"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "==== QuantG CI run ${STAMP} ===="
  cd "${APP_DIR}"

  echo "[1/2] measured knowledge, lesson migration, diagnostics, RAG, and Edge Lab queue"
  docker exec --user root "${SERVICE}" bash -lc 'cd /app && python /app/scripts/run_nightly_maintenance.py'

  echo "[2/2] durable research worker"
  "${APP_DIR}/scripts/research_worker.sh"

  echo "==== QuantG CI completed $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="
} >>"${LOG_PATH}" 2>&1
