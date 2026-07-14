"""Regression checks for closed-market chat and Dashboard behavior."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import dashboard_server
import etf_agent_chat
from agent_server import CHAT_HTML


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("OK:", message)


def _calendar(value: object) -> bool:
    if hasattr(value, "date") and not isinstance(value, date):
        value = value.date()  # type: ignore[union-attr]
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return value == date(2026, 7, 10)


def main() -> None:
    with mock.patch.object(dashboard_server, "is_trading_day", side_effect=_calendar):
        closed = dashboard_server.load_status()
        _assert(closed["market_closed"] is True, "Dashboard reports market closed")
        _assert(closed["full"] is None, "closed default does not reuse historical advice")
        _assert(closed["submit"] is None, "closed default has no submit payload")

        historical = dashboard_server.load_status("2026-07-10")
        _assert(historical["is_historical_view"] is True, "explicit date is historical view")
        _assert(historical["full"] is not None, "historical result remains viewable")

        prepared = dashboard_server._prepare_daily_job({"force": True})
        _assert(isinstance(prepared, dict), "closed run returns status response")
        _assert(prepared.get("status") == "market_closed", "closed run is blocked")

    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "2099-01-01_full.json").write_text("{}", encoding="utf-8")
        _assert(
            dashboard_server._latest_file("*_full.json", root) is None,
            "future output is never selected",
        )

        full = root / "2026-07-10_full.json"
        full.write_text(
            json.dumps(
                {
                    "competition_output": [
                        {"symbol": "510300", "symbol_name": "ETF", "volume": 100}
                    ]
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.object(dashboard_server, "shanghai_now", return_value=datetime(2026, 7, 10, 15, 30)), mock.patch.object(
            dashboard_server, "settlement_ready", return_value=False
        ), mock.patch.object(dashboard_server, "_load_bar") as load_bar:
            pending = dashboard_server._settle_prediction(full)
            _assert(pending["pending"] is True, "Dashboard waits for settlement readiness")
            load_bar.assert_not_called()

        with mock.patch.object(etf_agent_chat, "shanghai_now", return_value=datetime(2026, 7, 10, 15, 30)):
            _assert(not etf_agent_chat._market_closed_for_pnl(), "chat does not settle at 15:30")
        with mock.patch.object(etf_agent_chat, "shanghai_now", return_value=datetime(2026, 7, 10, 16, 30)):
            _assert(etf_agent_chat._market_closed_for_pnl(), "chat settles after 16:15")

    _assert("marketBanner" in CHAT_HTML, "chat contains market-closed banner")
    _assert("data-requires-open" in CHAT_HTML, "chat advice shortcuts are controllable")
    _assert('id="kbInfo"' not in CHAT_HTML, "header hides knowledge-base metadata")
    _assert('id="sessInfo"' not in CHAT_HTML, "header hides session and capital metadata")
    _assert("storageSet(SESS_KEY, sessionId)" in CHAT_HTML, "hidden session state is still persisted")
    print("MARKET CLOSED FRONTEND OK")


if __name__ == "__main__":
    main()
