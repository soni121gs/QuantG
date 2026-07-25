#!/usr/bin/env bash
set -euo pipefail

SERVICE="${QUANTG_BACKEND_SERVICE:-quantg-backend}"
LOCK_PATH="${QUANTG_RESEARCH_LOCK:-/var/lock/quantg_research_worker.lock}"

flock -n "${LOCK_PATH}" docker exec "${SERVICE}" \
  bash -lc 'cd /app && python /app/scripts/process_research_jobs.py'
