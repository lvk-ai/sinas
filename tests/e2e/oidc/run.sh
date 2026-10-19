#!/usr/bin/env bash
# End-to-end SSO test against a real Keycloak (docs/design/oidc-sso.md).
#
# Starts, all with their own names and nothing shared with another stack:
#   - Keycloak (start-dev) with the realm in realm.json: a confidential
#     client, a groups mapper, user "ann" in groups sinas-admins and
#     sinas-readers;
#   - Postgres, Redis and a Sinas backend from this checkout (the backend
#     image with ./backend mounted), sharing Keycloak's network namespace,
#     so http://localhost:18080 is Keycloak for the backend and for this
#     machine alike (one issuer URL for both).
# Then drive.py signs in through Keycloak's login form, checks the user is
# provisioned with the mapped roles, removes a group in Keycloak, signs in
# again and checks that role is gone (and a hand-granted one stays), and
# checks per-domain enforcement. Everything is removed afterwards
# (KEEP=1 leaves it running, e.g. to click through the console).
#
# Secrets (client secret, passwords) are generated per run, passed through
# the environment and never printed.
#
# Usage: tests/e2e/oidc/run.sh   (needs docker and python3)
#   BACKEND_IMAGE  backend image to run the code in (default ghcr.io/sinas-platform/sinas/backend:latest)
#   KC_IMAGE       default quay.io/keycloak/keycloak:26.0
#   CONSOLE_DIST   a built console (console/dist) to serve at /ui, optional
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
PREFIX="${PREFIX:-sinas-oidc-e2e}"
KC_PORT="${KC_PORT:-18080}"
SINAS_PORT="${SINAS_PORT:-18000}"
BACKEND_IMAGE="${BACKEND_IMAGE:-ghcr.io/sinas-platform/sinas/backend:latest}"
KC_IMAGE="${KC_IMAGE:-quay.io/keycloak/keycloak:26.0}"
WORK="$(mktemp -d)"

rand() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }
export KC_ADMIN_USER=admin KC_ADMIN_PASSWORD="$(rand)" KC_CLIENT_SECRET="$(rand)" KC_USER_PASSWORD="$(rand)"
export SUPERADMIN_EMAIL=admin@example.com SUPERADMIN_PASSWORD="$(rand)"
PG_PASSWORD="$(rand)"
SECRET_KEY="$(rand)"
ENCRYPTION_KEY="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"

cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then
    echo "KEEP=1: leaving ${PREFIX}-* running (docker rm -f -v ${PREFIX}-sinas ${PREFIX}-redis ${PREFIX}-pg ${PREFIX}-kc)"
  else
    docker rm -f -v "${PREFIX}-sinas" "${PREFIX}-redis" "${PREFIX}-pg" "${PREFIX}-kc" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT

wait_for() {  # url, what, tries
  for _ in $(seq 1 "${3:-90}"); do
    if curl -sf -o /dev/null "$1"; then return 0; fi
    sleep 2
  done
  echo "$2 did not come up"; return 1
}

# The realm, with this run's secrets and redirect URI filled in.
mkdir -p "$WORK/import"
python3 - "$HERE/realm.json" "$WORK/import/realm.json" "http://localhost:${SINAS_PORT}/auth/oidc/keycloak/callback" <<'EOF'
import os, sys
src, dst, redirect = sys.argv[1:4]
text = open(src).read()
text = text.replace("__CLIENT_SECRET__", os.environ["KC_CLIENT_SECRET"])
text = text.replace("__USER_PASSWORD__", os.environ["KC_USER_PASSWORD"])
text = text.replace("__REDIRECT_URI__", redirect)
open(dst, "w").write(text)
EOF
chmod -R a+rX "$WORK/import"

echo "Starting Keycloak (${KC_IMAGE}) on :${KC_PORT}"
docker run -d --name "${PREFIX}-kc" --memory 1200m \
  -p "127.0.0.1:${KC_PORT}:${KC_PORT}" -p "127.0.0.1:${SINAS_PORT}:${SINAS_PORT}" \
  -e KC_BOOTSTRAP_ADMIN_USERNAME="$KC_ADMIN_USER" -e KC_BOOTSTRAP_ADMIN_PASSWORD="$KC_ADMIN_PASSWORD" \
  -e JAVA_OPTS_KC_HEAP="-Xms256m -Xmx768m" \
  -v "$WORK/import:/opt/keycloak/data/import:ro" \
  "$KC_IMAGE" start-dev --import-realm --http-port="${KC_PORT}" >/dev/null

NET="container:${PREFIX}-kc"
docker run -d --name "${PREFIX}-pg" --network "$NET" --memory 256m \
  -e POSTGRES_PASSWORD="$PG_PASSWORD" -e POSTGRES_DB=sinas postgres:16-alpine >/dev/null
docker run -d --name "${PREFIX}-redis" --network "$NET" --memory 128m redis:7-alpine >/dev/null

for _ in $(seq 1 30); do
  docker exec "${PREFIX}-pg" pg_isready -U postgres -q && break
  sleep 1
done

CONSOLE_ARGS=()
if [ -n "${CONSOLE_DIST:-}" ]; then
  CONSOLE_ARGS=(-v "${CONSOLE_DIST}:/console-dist:ro" -e SERVE_CONSOLE=true -e CONSOLE_DIST_PATH=/console-dist)
fi

echo "Starting the Sinas backend from ${ROOT}/backend on :${SINAS_PORT}"
docker run -d --name "${PREFIX}-sinas" --network "$NET" --memory 768m \
  -v "${ROOT}/backend:/app" -w /app \
  -e DATABASE_HOST=localhost -e DATABASE_PORT=5432 -e DATABASE_USER=postgres \
  -e DATABASE_PASSWORD="$PG_PASSWORD" -e DATABASE_NAME=sinas \
  -e REDIS_URL=redis://localhost:6379/0 \
  -e SECRET_KEY="$SECRET_KEY" -e ENCRYPTION_KEY="$ENCRYPTION_KEY" \
  -e SUPERADMIN_EMAIL -e SUPERADMIN_PASSWORD -e AUTH_MODE=password \
  -e CODE_EXECUTION_ENABLED=false -e CLICKHOUSE_HOST= \
  -e BACKEND_PORT="${SINAS_PORT}" -e CONSOLE_URL="http://localhost:${SINAS_PORT}/ui" \
  ${CONSOLE_ARGS[@]+"${CONSOLE_ARGS[@]}"} \
  --entrypoint sh "$BACKEND_IMAGE" \
  -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${SINAS_PORT}" >/dev/null

wait_for "http://localhost:${KC_PORT}/realms/sinas-e2e/.well-known/openid-configuration" Keycloak 120
wait_for "http://localhost:${SINAS_PORT}/health" "Sinas backend" 90

SINAS_URL="http://localhost:${SINAS_PORT}" KC_URL="http://localhost:${KC_PORT}" python3 "$HERE/drive.py"
