#!/bin/sh
# Fail-closed start-up checks, then hand over to supervisord (which runs redis, api, worker, seed).
set -eu

fatal() { echo "FATAL: $1" >&2; exit 64; }

[ -n "${API_TOKEN:-}" ] || fatal "API_TOKEN is not set"
[ "${#API_TOKEN}" -ge 16 ] || fatal "API_TOKEN must be at least 16 characters"
[ "$API_TOKEN" != "change-me-admin-token" ] || fatal "API_TOKEN must not be the example default"
[ -n "${CALLBACK_SIGNING_SECRET:-}" ] || fatal "CALLBACK_SIGNING_SECRET is not set"
[ "$CALLBACK_SIGNING_SECRET" != "change-me-callback-secret" ] || fatal "CALLBACK_SIGNING_SECRET must not be the example default"
case "${SIMULATION_MODE:-true}" in
  true|TRUE|True|1) ;;
  *) fatal "this image only runs in SIMULATION_MODE=true (no real publishing)";;
esac

for v in CALLBACK_ALLOW_PRIVATE CALLBACK_ALLOW_HTTP MEDIA_FETCH_ALLOW_PRIVATE; do
  eval "val=\${$v:-false}"
  case "$val" in false|FALSE|False|0|"") ;; *) fatal "$v must not be enabled in this image (callback SSRF protection)";; esac
done
export CALLBACK_ALLOW_PRIVATE=false CALLBACK_ALLOW_HTTP=false MEDIA_FETCH_ALLOW_PRIVATE=false

export PORT="${PORT:-10000}"
export SIMULATION_MODE=true
export APP_ENV=production
export JOB_BACKEND=rq
export REDIS_URL="redis://127.0.0.1:6379/0"
export DATABASE_URL="${DATABASE_URL:-sqlite:////tmp/incubrix/demo.db}"
case "$DATABASE_URL" in sqlite:*) ;; *) fatal "this image expects a SQLite DATABASE_URL";; esac
export S3_ENDPOINT_URL=""
export LOCAL_STORE_DIR="${LOCAL_STORE_DIR:-/tmp/incubrix/store}"
export RATE_LIMIT_PER_MINUTE="${RATE_LIMIT_PER_MINUTE:-300}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-${RENDER_EXTERNAL_URL:-http://localhost:${PORT}}}"
unset GHOST_ADMIN_KEY LEMMY_PASSWORD DISCOURSE_API_KEY DEVTO_API_KEY WEBHOOK_SECRET 2>/dev/null || true

mkdir -p /tmp/incubrix/store /tmp/redis
chown -R app:app /tmp/incubrix /tmp/redis
echo "starting demo: simulation mode, port ${PORT}, public base ${PUBLIC_BASE_URL}"
exec /usr/bin/supervisord -c /srv/deploy/render/supervisord.conf
