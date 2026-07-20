#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
RUN_DIR="${ROOT}/run"
LOG_DIR="${ROOT}/logs"
PID_FILE="${RUN_DIR}/public_gateway.pid"
DASHBOARD_PID_FILE="${RUN_DIR}/dashboard.pid"
CHAT_PID_FILE="${RUN_DIR}/chat.pid"
LOG_FILE="${LOG_DIR}/public_gateway.log"
DASHBOARD_LOG_FILE="${LOG_DIR}/dashboard.log"
CHAT_LOG_FILE="${LOG_DIR}/chat.log"
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

owned_component_pid() {
    local script="$1"
    local proc pid owner cwd cmd
    for proc in /proc/[0-9]*; do
        pid="${proc##*/}"
        owner="$(stat -c '%u' "${proc}" 2>/dev/null || true)"
        [[ "${owner}" == "$(id -u)" ]] || continue
        cwd="$(readlink -f "${proc}/cwd" 2>/dev/null || true)"
        [[ "${cwd}" == "${ROOT}" ]] || continue
        cmd="$(tr '\0' ' ' < "${proc}/cmdline" 2>/dev/null || true)"
        if [[ "${cmd}" == *"${script}"* ]]; then
            echo "${pid}"
            return 0
        fi
    done
    return 1
}

stop_owned_component() {
    local script="$1"
    local pid
    pid="$(owned_component_pid "${script}" || true)"
    [[ -n "${pid}" ]] || return 0
    kill "${pid}"
    for _ in {1..20}; do
        kill -0 "${pid}" 2>/dev/null || return 0
        sleep 0.25
    done
    kill -KILL "${pid}"
}

restart_upstreams() {
    stop_owned_component "dashboard_server.py"
    stop_owned_component "agent_server.py"
    rm -f "${DASHBOARD_PID_FILE}" "${CHAT_PID_FILE}"

    (
        cd "${ROOT}"
        nohup "${PYTHON}" dashboard_server.py --host 127.0.0.1 --port 8765 --no-browser \
            >> "${DASHBOARD_LOG_FILE}" 2>&1 < /dev/null &
        echo "$!" > "${DASHBOARD_PID_FILE}"
        nohup "${PYTHON}" agent_server.py --host 127.0.0.1 --port 8766 --no-browser \
            >> "${CHAT_LOG_FILE}" 2>&1 < /dev/null &
        echo "$!" > "${CHAT_PID_FILE}"
    )
    sleep 1
    curl -fsS "http://127.0.0.1:8765/api/status" >/dev/null
    curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' \
        "http://127.0.0.1:8766/api/session/start" >/dev/null
}

start_gateway() {
    restart_upstreams
    if is_running; then
        echo "public gateway already running (pid=$(cat "${PID_FILE}"), port=${PORT})"
        return 0
    fi
    rm -f "${PID_FILE}"
    if ss -H -lnt | awk '{print $4}' | grep -Eq ":${PORT}$"; then
        echo "port ${PORT} is already in use" >&2
        return 1
    fi
    if [[ -f "${LOG_FILE}" ]] && (( $(stat -c '%s' "${LOG_FILE}") > 10485760 )); then
        mv -f "${LOG_FILE}" "${LOG_FILE}.1"
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
    curl -fsS "http://127.0.0.1:${PORT}/etf-agent/" >/dev/null
    curl -fsS "http://127.0.0.1:${PORT}/etf-agent/chat/" >/dev/null
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
        curl -fsS "http://127.0.0.1:${PORT}/etf-agent/" >/dev/null
        curl -fsS "http://127.0.0.1:${PORT}/etf-agent/chat/" >/dev/null
        echo
        echo "dashboard and chat upstreams are healthy"
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
