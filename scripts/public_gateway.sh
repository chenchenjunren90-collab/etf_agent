#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
RUN_DIR="${ROOT}/run"
LOG_DIR="${ROOT}/logs"
PID_FILE="${RUN_DIR}/public_gateway.pid"
LOG_FILE="${LOG_DIR}/public_gateway.log"
HOST="${ETF_PUBLIC_HOST:-0.0.0.0}"
PORT="${ETF_PUBLIC_PORT:-3004}"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

is_running() {
    [[ -f "${PID_FILE}" ]] || return 1
    local pid
    pid="$(cat "${PID_FILE}")"
    [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
    kill -0 "${pid}" 2>/dev/null || return 1
    tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q 'public_gateway.py'
}

start_gateway() {
    if is_running; then
        echo "public gateway already running (pid=$(cat "${PID_FILE}"), port=${PORT})"
        return 0
    fi
    rm -f "${PID_FILE}"
    if ss -H -lnt | awk '{print $4}' | grep -Eq ":${PORT}$"; then
        echo "port ${PORT} is already in use" >&2
        return 1
    fi
    nohup "${PYTHON}" "${ROOT}/public_gateway.py" --host "${HOST}" --port "${PORT}" \
        >> "${LOG_FILE}" 2>&1 < /dev/null &
    echo "$!" > "${PID_FILE}"
    sleep 1
    if ! is_running; then
        echo "public gateway failed to start; see ${LOG_FILE}" >&2
        tail -n 30 "${LOG_FILE}" >&2 || true
        return 1
    fi
    curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null
    echo "public gateway started (pid=$(cat "${PID_FILE}"), port=${PORT})"
}

stop_gateway() {
    if ! is_running; then
        rm -f "${PID_FILE}"
        echo "public gateway is not running"
        return 0
    fi
    local pid
    pid="$(cat "${PID_FILE}")"
    kill "${pid}"
    for _ in {1..20}; do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 0.25
    done
    if kill -0 "${pid}" 2>/dev/null; then
        kill -KILL "${pid}"
    fi
    rm -f "${PID_FILE}"
    echo "public gateway stopped"
}

status_gateway() {
    if is_running; then
        echo "public gateway running (pid=$(cat "${PID_FILE}"), port=${PORT})"
        curl -fsS "http://127.0.0.1:${PORT}/healthz"
    else
        echo "public gateway is not running"
        return 1
    fi
}

case "${1:-status}" in
    start) start_gateway ;;
    stop) stop_gateway ;;
    restart) stop_gateway; start_gateway ;;
    status) status_gateway ;;
    *) echo "usage: $0 {start|stop|restart|status}" >&2; exit 2 ;;
esac
