#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${QUANTG_APP_DIR:-/opt/QuantG}"
RUNNER="${APP_DIR}/scripts/ci_run.sh"
CRON_LINE="15 20 * * 1-5 QUANTG_APP_DIR=${APP_DIR} ${RUNNER}"

tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v "scripts/ci_run.sh" >"${tmp}" || true
printf '%s\n' "${CRON_LINE}" >>"${tmp}"
crontab "${tmp}"
rm -f "${tmp}"
echo "Installed QuantG CI cron: ${CRON_LINE}"
