#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${AGENT_CONSOLE_ENV_FILE:-}" ]]; then
  ENV_FILE="$AGENT_CONSOLE_ENV_FILE"
elif [[ -f .env.production ]]; then
  ENV_FILE=".env.production"
else
  ENV_FILE=".env"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "deployment environment file not found: $ENV_FILE" >&2
  exit 1
fi

COMPOSE=(
  docker compose
  --env-file "$ENV_FILE"
  -f docker-compose.yml
)

USE_PRODUCTION_OVERLAY="${AGENT_CONSOLE_USE_PRODUCTION_OVERLAY:-auto}"
case "$USE_PRODUCTION_OVERLAY" in
  true)
    COMPOSE+=(-f docker-compose.production.yml)
    ;;
  false)
    ;;
  auto)
    if [[ "$(basename "$ENV_FILE")" == ".env.production" ]]; then
      COMPOSE+=(-f docker-compose.production.yml)
    fi
    ;;
  *)
    echo "AGENT_CONSOLE_USE_PRODUCTION_OVERLAY must be auto, true, or false" >&2
    exit 1
    ;;
esac

# The server overlay is mandatory. A later host override may still refine
# ports or mounts, but the rendered-config gate below rejects any override
# that removes scheduler from the SDK network.
COMPOSE+=(-f docker-compose.server.yml)

SITE_OVERRIDE="${AGENT_CONSOLE_SITE_OVERRIDE_FILE:-docker-compose.override.yml}"
if [[ "$SITE_OVERRIDE" != "none" && -f "$SITE_OVERRIDE" ]]; then
  COMPOSE+=(-f "$SITE_OVERRIDE")
fi

if [[ -n "${AGENT_CONSOLE_PROJECT_NAME:-}" ]]; then
  COMPOSE+=(--project-name "$AGENT_CONSOLE_PROJECT_NAME")
fi

CONFIG_JSON="$(mktemp)"
trap 'rm -f "$CONFIG_JSON"' EXIT

"${COMPOSE[@]}" --profile app --profile wxbot config --format json >"$CONFIG_JSON"

SDK_NETWORK="$(
  python3 - "$CONFIG_JSON" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)

scheduler = config.get("services", {}).get("scheduler", {})
networks = scheduler.get("networks", {})
network_names = set(networks if isinstance(networks, dict) else networks or [])
if "wxbot-sdk" not in network_names:
    raise SystemExit(
        "refusing deployment: merged scheduler config is missing wxbot-sdk"
    )

sdk_network = config.get("networks", {}).get("wxbot-sdk", {})
resolved_name = str(sdk_network.get("name") or "").strip()
if not resolved_name:
    raise SystemExit(
        "refusing deployment: merged wxbot-sdk network has no resolved name"
    )
print(resolved_name)
PY
)"

if ! docker network inspect "$SDK_NETWORK" >/dev/null 2>&1; then
  echo "required external Docker network does not exist: $SDK_NETWORK" >&2
  exit 1
fi

echo "Preflight passed: scheduler will join $SDK_NETWORK"

"${COMPOSE[@]}" --profile app --profile wxbot build
"${COMPOSE[@]}" --profile app run --rm migrate
"${COMPOSE[@]}" --profile app --profile wxbot up \
  -d \
  --remove-orphans \
  --wait \
  --wait-timeout "${AGENT_CONSOLE_DEPLOY_WAIT_SECONDS:-300}"

SCHEDULER_ID="$("${COMPOSE[@]}" --profile app ps -q scheduler)"
if [[ -z "$SCHEDULER_ID" ]]; then
  echo "scheduler container was not created" >&2
  exit 1
fi

if ! docker inspect "$SCHEDULER_ID" \
  --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}' |
  grep -Fxq "$SDK_NETWORK"; then
  echo "scheduler is not attached to $SDK_NETWORK after deployment" >&2
  exit 1
fi

"${COMPOSE[@]}" --profile app exec -T scheduler python - <<'PY'
import os

import httpx

url = os.environ["WXBOT_SDK_URL"].rstrip("/") + "/status"
response = httpx.get(url, timeout=5.0, trust_env=False)
response.raise_for_status()
print(f"scheduler -> wxbot SDK: {url} HTTP {response.status_code}")
PY

"${COMPOSE[@]}" --profile app --profile wxbot ps
echo "Server deployment completed with scheduler SDK connectivity verified."
