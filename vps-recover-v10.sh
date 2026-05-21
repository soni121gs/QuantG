#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/QuantG}"
REPO_URL="${REPO_URL:-https://github.com/soni121gs/QuantG.git}"
BRANCH="${BRANCH:-main}"

echo "== QuantG v10 recovery deploy =="
echo "App dir: ${APP_DIR}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker first." >&2
  exit 1
fi

mkdir -p /root
BACKUP_DIR="/root/quantg-backup-$(date +%F-%H%M)"
mkdir -p "$BACKUP_DIR"

if [ -f "${APP_DIR}/backend/.env" ]; then
  cp "${APP_DIR}/backend/.env" "${BACKUP_DIR}/backend.env"
  echo "Backed up backend .env to ${BACKUP_DIR}/backend.env"
fi

if docker ps --format '{{.Names}}' | grep -qx 'quantg-mongo'; then
  docker exec quantg-mongo mongodump --archive=/tmp/quantg.archive --db quantg || true
  docker cp quantg-mongo:/tmp/quantg.archive "${BACKUP_DIR}/quantg.archive" || true
  echo "Backed up Mongo archive to ${BACKUP_DIR}/quantg.archive"
fi

if [ ! -d "${APP_DIR}/.git" ]; then
  rm -rf "$APP_DIR"
  git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/${BRANCH}"

echo "Current commits:"
git log --oneline -5

if [ ! -f backend/.env ]; then
  LATEST_ENV="$(ls -t /root/quantg-backup-*/backend.env 2>/dev/null | head -1 || true)"
  if [ -n "$LATEST_ENV" ]; then
    cp "$LATEST_ENV" backend/.env
    echo "Restored backend/.env from $LATEST_ENV"
  fi
fi

if [ ! -f backend/.env ]; then
  echo "backend/.env is missing. Create it before starting the app." >&2
  exit 1
fi

echo "Frontend Docker context size check:"
du -sh frontend
echo "frontend/.dockerignore:"
cat frontend/.dockerignore

docker compose down --remove-orphans || true
docker compose build --no-cache --progress=plain backend frontend
docker compose up -d --force-recreate
docker compose ps

echo "Verifying backend:"
docker compose exec -T backend python - <<'PY'
import server
print("APP_VERSION", server.APP_VERSION)
PY

echo "Verifying frontend bundle markers:"
docker compose exec -T frontend sh -c "grep -R 'Standardized Strategies' /usr/share/nginx/html/static/js >/dev/null && echo 'FOUND Standardized Strategies' || echo 'MISSING Standardized Strategies'"
docker compose exec -T frontend sh -c "grep -R 'v10.0' /usr/share/nginx/html/static/js >/dev/null && echo 'FOUND v10.0' || echo 'MISSING v10.0'"
docker compose exec -T frontend sh -c "grep -R 'Visual Builder' /usr/share/nginx/html/static/js >/dev/null && echo 'FOUND Visual Builder' || true"

echo "Done. Open https://www.quantgtrade.com/strategies?fresh=v10 and hard-refresh."
echo "Backups kept in ${BACKUP_DIR}"
