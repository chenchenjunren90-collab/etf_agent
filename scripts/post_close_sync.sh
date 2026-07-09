#!/usr/bin/env bash
# 交易日闭市后同步 ETF 行情（供仪表盘收益结算）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

export PYTHONUTF8=1
export ETF_AGENT_ALLOW_NETWORK=1

exec "$ROOT/.venv/bin/python" post_close_sync.py
