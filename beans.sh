#!/usr/bin/env bash
# Manage the Beans Proxy server (start / stop / status).
#
# Default mode is "start in the background" — no flags required.
# Configuration is resolved through the same Python settings loader used by
# `python -m beans_proxy`.
#
# Usage:
#   ./beans.sh                   # START in the background (default)
#   ./beans.sh start             # same as above, explicit
#   ./beans.sh --foreground      # run in the foreground (Ctrl-C to stop)
#   ./beans.sh stop              # stop a previously started background instance
#   ./beans.sh status            # report whether the proxy is up
#   ./beans.sh restart           # stop, then start in the background
#
# Env (all optional; environment variables override values in .env):
#   BEANS_PROXY_HOST    bind host (default 127.0.0.1)
#   BEANS_PROXY_PORT    bind port (default 8000)
#   BEANS_PROXY_LOG_FILE log file path (default beans_proxy.log)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PID_FILE="${SCRIPT_DIR}/.beans_proxy.pid"

usage() {
  sed -n '2,/^[^#]/p' "$0" | sed '$d'
}

_healthz() {
  curl --silent --fail --max-time 2 "${PROXY_URL}/healthz" >/dev/null 2>&1
}

_is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

_load_runtime_config() {
  local config_output
  if ! config_output="$(python -c '
from beans_proxy.config import load_settings

settings = load_settings()
print(settings.host)
print(settings.port)
print(settings.log_file)
')"; then
    echo "Beans Proxy configuration is invalid" >&2
    exit 1
  fi

  local config_values=()
  while IFS= read -r value; do
    config_values+=("$value")
  done <<<"$config_output"
  if [[ ${#config_values[@]} -ne 3 ]]; then
    echo "Beans Proxy configuration returned unexpected output" >&2
    exit 1
  fi

  PROXY_HOST="${config_values[0]}"
  PROXY_PORT="${config_values[1]}"
  LOG_FILE="${config_values[2]}"
  PROXY_URL="http://${PROXY_HOST}:${PROXY_PORT}"
}

MODE="start"
for arg in "$@"; do
  case "$arg" in
    start)            MODE="start" ;;
    stop|--stop)      MODE="stop" ;;
    restart|--restart) MODE="restart" ;;
    status|--status)  MODE="status" ;;
    -f|--foreground)  MODE="foreground" ;;
    -h|--help)        usage; exit 0 ;;
    *) echo "unknown arg: $arg" >&2; usage >&2; exit 64 ;;
  esac
done

# Activate the local virtualenv if it exists.
if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "warning: .venv not found, falling back to system python" >&2
fi

_load_runtime_config

case "$MODE" in
  status)
    if _healthz; then
      echo "Beans Proxy is up at ${PROXY_URL}"
      [[ -f "$PID_FILE" ]] && echo "  pid:  $(cat "$PID_FILE")"
      exit 0
    fi
    if _is_running; then
      echo "Beans Proxy process is running (pid $(cat "$PID_FILE")), but its health check failed at ${PROXY_URL}" >&2
      exit 1
    fi
    echo "Beans Proxy is not responding at ${PROXY_URL}" >&2
    exit 1
    ;;

  stop)
    if ! _is_running; then
      echo "Beans Proxy is not running"
      rm -f "$PID_FILE"
      exit 0
    fi
    stop_pid="$(cat "$PID_FILE")"
    echo "Stopping Beans Proxy (pid ${stop_pid})..."
    kill "$stop_pid" 2>/dev/null || true
    for _ in $(seq 1 50); do
      kill -0 "$stop_pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$stop_pid" 2>/dev/null; then
      echo "  process did not exit, sending SIGKILL" >&2
      kill -9 "$stop_pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "stopped"
    ;;

  foreground)
    exec python -m beans_proxy
    ;;

  start)
    if _healthz; then
      echo "Beans Proxy already running at ${PROXY_URL}"
      [[ -f "$PID_FILE" ]] && echo "  pid:  $(cat "$PID_FILE")"
      exit 0
    fi
    # Stale pid file with no live process — clear it.
    [[ -f "$PID_FILE" ]] && ! _is_running && rm -f "$PID_FILE"
    if _is_running; then
      echo "Beans Proxy process is already running (pid $(cat "$PID_FILE")), but its health check failed at ${PROXY_URL}" >&2
      echo "Not starting a second process; check ${LOG_FILE}" >&2
      exit 1
    fi

    echo "Starting Beans Proxy at ${PROXY_URL}"
    echo "  log:  ${LOG_FILE}"
    echo "  pid:  ${PID_FILE}"

    nohup python -m beans_proxy >>"$LOG_FILE" 2>&1 &
    PID=$!
    echo "$PID" >"$PID_FILE"

    # Pricing initialization can take up to 30 seconds before /healthz is ready.
    for _ in $(seq 1 175); do
      if _healthz; then
        echo "Beans Proxy ready at ${PROXY_URL} (pid ${PID}); running in background"
        exit 0
      fi
      if ! kill -0 "$PID" 2>/dev/null; then
        echo "Beans Proxy exited during startup; tail of log:" >&2
        tail -n 40 "$LOG_FILE" >&2 || true
        rm -f "$PID_FILE"
        exit 1
      fi
      sleep 0.2
    done

    echo "Beans Proxy process is still running (pid ${PID}), but did not become healthy at ${PROXY_URL} within 35s" >&2
    echo "The process was left running; tail of log:" >&2
    tail -n 40 "$LOG_FILE" >&2 || true
    exit 1
    ;;

  restart)
    "$0" stop || true
    "$0" start
    ;;
esac
