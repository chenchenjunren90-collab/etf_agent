"""Request guards: client IP, rate limits, admin token checks."""

from __future__ import annotations

import os
import math
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

_lock = threading.Lock()
_hits: dict[str, list[float]] = defaultdict(list)


def client_ip(handler: BaseHTTPRequestHandler) -> str:
    forwarded = handler.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = handler.headers.get("X-Real-IP", "")
    if real:
        return real.strip()
    host, *_ = handler.client_address
    return str(host)


def is_loopback(ip: str) -> bool:
    return ip in ("127.0.0.1", "::1", "localhost")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def rate_limit(
    key: str,
    *,
    max_calls: int,
    window_sec: float,
    min_interval_sec: float = 0,
) -> tuple[bool, str, int]:
    now = time.time()
    with _lock:
        times = [t for t in _hits[key] if now - t < window_sec]
        if times and min_interval_sec > 0 and (now - times[-1]) < min_interval_sec:
            wait = max(1, math.ceil(min_interval_sec - (now - times[-1])))
            return False, f"操作太频繁，请 {wait} 秒后再试", wait
        if len(times) >= max_calls:
            window_min = max(1, int(window_sec / 60))
            wait = max(1, math.ceil(window_sec - (now - times[0])))
            return False, f"已达到限制（{max_calls} 次/{window_min} 分钟），请稍后再试", wait
        times.append(now)
        _hits[key] = times
    return True, "", 0


def admin_token_ok(handler: BaseHTTPRequestHandler, body: dict[str, Any] | None = None) -> bool:
    expected = os.environ.get("ETF_RUN_TOKEN", "").strip()
    if not expected:
        return is_loopback(client_ip(handler))
    body = body or {}
    supplied = (
        handler.headers.get("X-ETF-Run-Token", "")
        or str(body.get("run_token") or "")
    ).strip()
    return supplied == expected


def check_api_run(handler: BaseHTTPRequestHandler, body: dict[str, Any]) -> dict[str, Any] | None:
    ip = client_ip(handler)
    max_calls = _env_int("ETF_RUN_MAX_PER_WINDOW", 3)
    window_sec = float(_env_int("ETF_RUN_WINDOW_SEC", 7200))
    min_interval = float(_env_int("ETF_RUN_MIN_INTERVAL_SEC", 180))

    ok, msg, retry_after = rate_limit(
        f"run:{ip}",
        max_calls=max_calls,
        window_sec=window_sec,
        min_interval_sec=min_interval,
    )
    if not ok:
        return {"status": "rate_limited", "output": msg, "retry_after": retry_after}

    if body.get("force") and not admin_token_ok(handler, body):
        return {
            "status": "forbidden",
            "output": "强制重跑需要管理员口令。请在弹窗中输入 run_token，或仅在本机直接访问仪表盘。",
        }
    return None


def check_chat(
    handler: BaseHTTPRequestHandler,
    body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    ip = client_ip(handler)
    body = body or {}
    session_id = str(body.get("session_id") or "anonymous")[:80]
    max_calls = _env_int("ETF_CHAT_MAX_PER_WINDOW", 40)
    window_sec = float(_env_int("ETF_CHAT_WINDOW_SEC", 600))
    min_interval = _env_float("ETF_CHAT_SESSION_MIN_INTERVAL_SEC", 0.25)

    # A short per-session interval catches duplicate clicks without making all
    # users behind the same NAT share one conversational cooldown.
    ok, msg, retry_after = rate_limit(
        f"chat-session:{ip}:{session_id}",
        max_calls=max_calls,
        window_sec=window_sec,
        min_interval_sec=min_interval,
    )
    if not ok:
        return {
            "error": msg,
            "reply": msg,
            "intent": "rate_limited",
            "retry_after": retry_after,
        }

    ip_max_calls = _env_int("ETF_CHAT_IP_MAX_PER_WINDOW", 120)
    ok, msg, retry_after = rate_limit(
        f"chat-ip:{ip}",
        max_calls=ip_max_calls,
        window_sec=window_sec,
    )
    if not ok:
        return {
            "error": msg,
            "reply": msg,
            "intent": "rate_limited",
            "retry_after": retry_after,
        }
    return None


def check_admin_action(handler: BaseHTTPRequestHandler, body: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if admin_token_ok(handler, body):
        return None
    return {
        "ok": False,
        "error": "需要管理员口令",
        "reply": "该操作需要管理员口令，已拒绝。",
        "intent": "forbidden",
    }
