"""Regression tests for conversational rate limits."""

from __future__ import annotations

from unittest.mock import patch

import security_guard


class FakeHandler:
    def __init__(self, ip: str = "203.0.113.10") -> None:
        self.headers = {"X-Forwarded-For": ip}
        self.client_address = (ip, 12345)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("OK:", message)


def main() -> None:
    handler = FakeHandler()
    env = {
        "ETF_CHAT_MAX_PER_WINDOW": "3",
        "ETF_CHAT_WINDOW_SEC": "60",
        "ETF_CHAT_SESSION_MIN_INTERVAL_SEC": "0.25",
        "ETF_CHAT_IP_MAX_PER_WINDOW": "10",
    }
    with patch.dict(security_guard.os.environ, env, clear=False):
        security_guard._hits.clear()
        with patch.object(security_guard.time, "time", return_value=100.0):
            first = security_guard.check_chat(handler, {"session_id": "session-a"})
        _assert(first is None, "first message is accepted")

        with patch.object(security_guard.time, "time", return_value=100.1):
            duplicate = security_guard.check_chat(handler, {"session_id": "session-a"})
            other_user = security_guard.check_chat(handler, {"session_id": "session-b"})
        _assert(duplicate is not None, "rapid duplicate in one session is limited")
        _assert(duplicate.get("retry_after") == 1, "retry delay is machine-readable")
        _assert(other_user is None, "users behind one IP do not share a cooldown")

        with patch.object(security_guard.time, "time", return_value=100.3):
            retried = security_guard.check_chat(handler, {"session_id": "session-a"})
        _assert(retried is None, "normal follow-up succeeds after short cooldown")

    print("ALL CHAT RATE-LIMIT TESTS PASSED")


if __name__ == "__main__":
    main()
