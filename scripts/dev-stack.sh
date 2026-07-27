#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/.run}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/.runlogs}"
VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python}"
UVICORN_BIN="${UVICORN_BIN:-$VENV_DIR/bin/uvicorn}"
HOST="${HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
ADMIN_BEARER_TOKEN="${ADMIN_BEARER_TOKEN:-}"
ADMIN_SESSION_SIGNING_SECRET="${ADMIN_SESSION_SIGNING_SECRET:-}"
MEDIA_ID_SIGNING_SECRET="${MEDIA_ID_SIGNING_SECRET:-}"
ADMIN_BEARER_TOKEN_FILE="${ADMIN_BEARER_TOKEN_FILE:-$RUN_DIR/admin-bearer-token}"
ADMIN_SESSION_SIGNING_SECRET_FILE="${ADMIN_SESSION_SIGNING_SECRET_FILE:-$RUN_DIR/admin-session-signing-secret}"
MEDIA_ID_SIGNING_SECRET_FILE="${MEDIA_ID_SIGNING_SECRET_FILE:-$RUN_DIR/media-id-signing-secret}"
LOCAL_NO_PROXY_DEFAULT="127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
LOCAL_NO_PROXY="${LOCAL_NO_PROXY:-$LOCAL_NO_PROXY_DEFAULT}"

CORE_SERVICES=(api frontend inbound outbound scheduler)
ALL_SERVICES=("${CORE_SERVICES[@]}" wxbot-bridge)

usage() {
  cat <<'EOF'
Usage: scripts/dev-stack.sh <command> [service...]

Commands:
  start       Start services that are not already running
  stop        Stop managed services
  restart     Stop then start services
  status      Show pid/log/running state
  health      Check API health, readiness, and frontend; include
              wxbot-bridge explicitly to check the optional adapter
  logs        Tail service logs

Services:
  api frontend inbound outbound scheduler wxbot-bridge workers all

With no service argument, commands operate on the core platform only. The
wxbot-bridge adapter is opt-in (`start wxbot-bridge`) or included by `all`.

Environment:
  HOST=127.0.0.1 API_PORT=8000 FRONTEND_PORT=5173
  VENV_DIR=.venv RUN_DIR=.run LOG_DIR=.runlogs
  ADMIN_BEARER_TOKEN=<32+ character secret; generated when omitted>
  ADMIN_BEARER_TOKEN_FILE=.run/admin-bearer-token
  LOCAL_NO_PROXY=127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16
EOF
}

prepend_no_proxy() {
  local existing="$1"
  if [[ -n "$existing" ]]; then
    printf '%s,%s' "$LOCAL_NO_PROXY" "$existing"
  else
    printf '%s' "$LOCAL_NO_PROXY"
  fi
}

ensure_dirs() {
  mkdir -p "$RUN_DIR" "$LOG_DIR"
}

ensure_dev_secret() {
  local variable_name="$1"
  local secret_file="$2"
  local current_value="${!variable_name:-}"

  if [[ -n "$current_value" ]]; then
    if [[ "${#current_value}" -lt 32 ]]; then
      echo "$variable_name must contain at least 32 characters" >&2
      return 1
    fi
  elif [[ -f "$secret_file" ]]; then
    current_value="$(<"$secret_file")"
    if [[ "${#current_value}" -lt 32 ]]; then
      echo "$secret_file does not contain a valid 32+ character secret" >&2
      return 1
    fi
    chmod 600 "$secret_file"
  else
    if [[ ! -x "$PYTHON_BIN" ]]; then
      echo "missing python: $PYTHON_BIN; run make install first" >&2
      return 1
    fi
    current_value="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    umask 077
    printf '%s\n' "$current_value" > "$secret_file"
    chmod 600 "$secret_file"
  fi

  printf -v "$variable_name" '%s' "$current_value"
  export "$variable_name"
}

ensure_dev_secrets() {
  ensure_dirs
  ensure_dev_secret ADMIN_BEARER_TOKEN "$ADMIN_BEARER_TOKEN_FILE"
  ensure_dev_secret \
    ADMIN_SESSION_SIGNING_SECRET \
    "$ADMIN_SESSION_SIGNING_SECRET_FILE"
  ensure_dev_secret MEDIA_ID_SIGNING_SECRET "$MEDIA_ID_SIGNING_SECRET_FILE"
  if [[ "$ADMIN_BEARER_TOKEN" == "$ADMIN_SESSION_SIGNING_SECRET" \
    || "$ADMIN_BEARER_TOKEN" == "$MEDIA_ID_SIGNING_SECRET" \
    || "$ADMIN_SESSION_SIGNING_SECRET" == "$MEDIA_ID_SIGNING_SECRET" ]]; then
    echo "development admin, session, and media secrets must be independent" >&2
    return 1
  fi
}

pid_file_for() {
  case "$1" in
    api) echo "$RUN_DIR/api-${API_PORT}.pid" ;;
    frontend) echo "$RUN_DIR/frontend-${FRONTEND_PORT}.pid" ;;
    inbound) echo "$RUN_DIR/inbound-worker.pid" ;;
    outbound) echo "$RUN_DIR/outbound-worker.pid" ;;
    scheduler) echo "$RUN_DIR/scheduler-worker.pid" ;;
    wxbot-bridge) echo "$RUN_DIR/wxbot-bridge-worker.pid" ;;
    *) return 1 ;;
  esac
}

log_file_for() {
  case "$1" in
    api) echo "$LOG_DIR/api.log" ;;
    frontend) echo "$LOG_DIR/frontend.log" ;;
    inbound) echo "$LOG_DIR/inbound.log" ;;
    outbound) echo "$LOG_DIR/outbound.log" ;;
    scheduler) echo "$LOG_DIR/scheduler.log" ;;
    wxbot-bridge) echo "$RUN_DIR/wxbot-bridge-worker.log" ;;
    *) return 1 ;;
  esac
}

service_label() {
  case "$1" in
    api) echo "api" ;;
    frontend) echo "frontend" ;;
    inbound) echo "inbound-worker" ;;
    outbound) echo "outbound-worker" ;;
    scheduler) echo "scheduler-worker" ;;
    wxbot-bridge) echo "wxbot-bridge-worker" ;;
    *) echo "$1" ;;
  esac
}

is_running_pid() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

is_running_service() {
  local pid_file pid
  pid_file="$(pid_file_for "$1")"
  [[ -f "$pid_file" ]] || return 1
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  is_running_pid "$pid"
}

expand_services() {
  if [[ "$#" -eq 0 ]]; then
    printf '%s\n' "${CORE_SERVICES[@]}"
    return
  fi
  local item
  for item in "$@"; do
    case "$item" in
      all)
        printf '%s\n' "${ALL_SERVICES[@]}"
        ;;
      workers)
        printf '%s\n' inbound outbound scheduler
        ;;
      bridge|wxbot_bridge|wxbot-bridge-worker)
        printf '%s\n' wxbot-bridge
        ;;
      inbound-worker)
        printf '%s\n' inbound
        ;;
      outbound-worker)
        printf '%s\n' outbound
        ;;
      scheduler-worker)
        printf '%s\n' scheduler
        ;;
      api|frontend|inbound|outbound|scheduler|wxbot-bridge)
        printf '%s\n' "$item"
        ;;
      *)
        echo "unknown service: $item" >&2
        return 2
        ;;
    esac
  done | awk '!seen[$0]++'
}

start_process() {
  local service="$1"
  local workdir="$2"
  shift 2
  local pid_file log_file pid label
  pid_file="$(pid_file_for "$service")"
  log_file="$(log_file_for "$service")"
  label="$(service_label "$service")"

  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if is_running_pid "$pid"; then
      printf '%-22s already running pid=%s log=%s\n' "$label" "$pid" "$log_file"
      return
    fi
    rm -f "$pid_file"
  fi

  : > "$log_file"
  (
    cd "$workdir"
    export NO_PROXY="$(prepend_no_proxy "${NO_PROXY:-}")"
    export no_proxy="$(prepend_no_proxy "${no_proxy:-}")"
    setsid "$@" >> "$log_file" 2>&1 &
    echo "$!" > "$pid_file"
  )
  pid="$(cat "$pid_file")"
  printf '%-22s started pid=%s log=%s\n' "$label" "$pid" "$log_file"
}

start_service() {
  ensure_dirs
  case "$1" in
    api)
      if [[ ! -x "$UVICORN_BIN" ]]; then
        echo "missing uvicorn: $UVICORN_BIN; run make install first" >&2
        return 1
      fi
      start_process api "$ROOT_DIR" "$UVICORN_BIN" app.main:app --host "$HOST" --port "$API_PORT"
      ;;
    frontend)
      if [[ ! -d "$ROOT_DIR/frontend/node_modules" ]]; then
        echo "missing frontend/node_modules; run make frontend-install first" >&2
        return 1
      fi
      start_process frontend "$ROOT_DIR/frontend" ./node_modules/.bin/vite --host "$HOST" --port "$FRONTEND_PORT"
      ;;
    inbound)
      if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "missing python: $PYTHON_BIN; run make install first" >&2
        return 1
      fi
      start_process inbound "$ROOT_DIR" "$PYTHON_BIN" -m app.workers.inbound_worker
      ;;
    outbound)
      if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "missing python: $PYTHON_BIN; run make install first" >&2
        return 1
      fi
      start_process outbound "$ROOT_DIR" "$PYTHON_BIN" -m app.workers.outbound_worker
      ;;
    scheduler)
      if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "missing python: $PYTHON_BIN; run make install first" >&2
        return 1
      fi
      start_process scheduler "$ROOT_DIR" "$PYTHON_BIN" -m app.workers.scheduler_worker
      ;;
    wxbot-bridge)
      if [[ ! -x "$PYTHON_BIN" ]]; then
        echo "missing python: $PYTHON_BIN; run make install first" >&2
        return 1
      fi
      start_process wxbot-bridge "$ROOT_DIR" "$PYTHON_BIN" -m app.workers.wxbot_bridge_worker
      ;;
  esac
}

stop_service() {
  local service="$1"
  local pid_file pid pgid label
  pid_file="$(pid_file_for "$service")"
  label="$(service_label "$service")"
  if [[ ! -f "$pid_file" ]]; then
    printf '%-22s stopped\n' "$label"
    return
  fi
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if ! is_running_pid "$pid"; then
    rm -f "$pid_file"
    printf '%-22s stopped stale_pid=%s\n' "$label" "${pid:-unknown}"
    return
  fi

  pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [[ -n "$pgid" ]]; then
    kill -TERM "-$pgid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
  fi

  for _ in {1..30}; do
    if ! is_running_pid "$pid"; then
      rm -f "$pid_file"
      printf '%-22s stopped pid=%s\n' "$label" "$pid"
      return
    fi
    sleep 0.2
  done

  if [[ -n "$pgid" ]]; then
    kill -KILL "-$pgid" 2>/dev/null || true
  else
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  printf '%-22s killed pid=%s\n' "$label" "$pid"
}

status_service() {
  local service="$1"
  local pid_file log_file pid label state
  pid_file="$(pid_file_for "$service")"
  log_file="$(log_file_for "$service")"
  label="$(service_label "$service")"
  pid=""
  state="stopped"
  if [[ -f "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if is_running_pid "$pid"; then
      state="running"
    else
      state="stale"
    fi
  fi
  printf '%-22s %-8s pid=%-8s log=%s\n' "$label" "$state" "${pid:-"-"}" "$log_file"
}

health_check() {
  local services=("$@")
  local ok=0
  echo "api health:"
  curl_retry "http://127.0.0.1:${API_PORT}/healthz" || ok=1
  echo
  echo "api ready:"
  curl_retry "http://127.0.0.1:${API_PORT}/readyz" || ok=1
  echo
  local service
  for service in "${services[@]}"; do
    if [[ "$service" == "wxbot-bridge" ]]; then
      echo "wxbot bridge:"
      curl_retry "http://127.0.0.1:${API_PORT}/plugins/wxbot/bridge/status" || ok=1
      echo
      break
    fi
  done
  echo "frontend:"
  curl_retry "http://127.0.0.1:${FRONTEND_PORT}/" >/dev/null && echo "ok" || ok=1
  return "$ok"
}

curl_retry() {
  local url="$1"
  local attempt
  for attempt in {1..20}; do
    if curl --noproxy '*' --max-time 5 -fsS "$url"; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

tail_logs() {
  local services=("$@")
  local files=()
  local service file
  for service in "${services[@]}"; do
    file="$(log_file_for "$service")"
    [[ -f "$file" ]] && files+=("$file")
  done
  if [[ "${#files[@]}" -eq 0 ]]; then
    echo "no log files found" >&2
    return 1
  fi
  tail -n 80 -f "${files[@]}"
}

main() {
  local command="${1:-}"
  if [[ -z "$command" || "$command" == "-h" || "$command" == "--help" ]]; then
    usage
    return 0
  fi
  shift || true

  local services=()
  local services_text
  services_text="$(expand_services "$@")" || return "$?"
  mapfile -t services <<< "$services_text"

  case "$command" in
    start)
      ensure_dev_secrets
      if [[ -f "$ADMIN_BEARER_TOKEN_FILE" ]]; then
        echo "admin token: $ADMIN_BEARER_TOKEN_FILE"
      else
        echo "admin token: supplied through ADMIN_BEARER_TOKEN"
      fi
      local service
      for service in "${services[@]}"; do
        start_service "$service"
      done
      ;;
    stop)
      local service
      for ((i=${#services[@]}-1; i>=0; i--)); do
        stop_service "${services[$i]}"
      done
      ;;
    restart)
      "$0" stop "${services[@]}"
      "$0" start "${services[@]}"
      ;;
    status)
      local service
      for service in "${services[@]}"; do
        status_service "$service"
      done
      ;;
    health)
      health_check "${services[@]}"
      ;;
    logs)
      tail_logs "${services[@]}"
      ;;
    *)
      echo "unknown command: $command" >&2
      usage >&2
      return 2
      ;;
  esac
}

main "$@"
