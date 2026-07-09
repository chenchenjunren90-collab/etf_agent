"""In-memory session store for the conversational ETF advisor."""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any


# Session TTL: 2 hours of inactivity
SESSION_TTL_SEC = 2 * 60 * 60

# States
STATE_IDLE = "idle"
STATE_COLLECTING = "collecting"
STATE_READY = "ready"
STATE_DONE = "done"

_lock = threading.Lock()
_sessions: dict[str, dict[str, Any]] = {}


def _now() -> float:
    return time.time()


def create_session() -> dict[str, Any]:
    sid = uuid.uuid4().hex[:16]
    sess = {
        "session_id": sid,
        "state": STATE_IDLE,
        "profile": {},
        "collect_step": None,
        "advice_mode": "personal",  # personal | competition
        "last_advice": None,
        "messages": [],
        "created_at": _now(),
        "updated_at": _now(),
    }
    with _lock:
        _sessions[sid] = sess
        _purge_expired_unlocked()
    return dict(sess)


def get_session(session_id: str | None) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _lock:
        _purge_expired_unlocked()
        sess = _sessions.get(session_id)
        if not sess:
            return None
        sess["updated_at"] = _now()
        return dict(sess)


def update_session(session_id: str, **kwargs: Any) -> dict[str, Any] | None:
    with _lock:
        sess = _sessions.get(session_id)
        if not sess:
            return None
        for k, v in kwargs.items():
            if k == "profile" and isinstance(v, dict):
                sess.setdefault("profile", {}).update(v)
            elif k == "append_message" and isinstance(v, dict):
                sess.setdefault("messages", []).append(v)
                # keep last 40 turns
                if len(sess["messages"]) > 40:
                    sess["messages"] = sess["messages"][-40:]
            else:
                sess[k] = v
        sess["updated_at"] = _now()
        return dict(sess)


def ensure_session(session_id: str | None) -> dict[str, Any]:
    sess = get_session(session_id)
    if sess:
        return sess
    return create_session()


def _purge_expired_unlocked() -> None:
    cutoff = _now() - SESSION_TTL_SEC
    dead = [sid for sid, s in _sessions.items() if s.get("updated_at", 0) < cutoff]
    for sid in dead:
        _sessions.pop(sid, None)


def public_view(sess: dict[str, Any]) -> dict[str, Any]:
    """Safe subset for API responses."""
    return {
        "session_id": sess.get("session_id"),
        "state": sess.get("state"),
        "profile": sess.get("profile") or {},
        "collect_step": sess.get("collect_step"),
        "advice_mode": sess.get("advice_mode"),
        "has_advice": sess.get("last_advice") is not None,
    }
