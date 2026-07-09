"""Verify competition isolation: personal/chat must not overwrite official outputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest import mock

from competition_guard import (
    COMPETITION_CAPITAL,
    chat_force_allowed,
    guard_chat_prediction_run,
    is_competition_capital,
    should_write_competition_artifacts,
)
from personalized_advisor import build_personal_advice


BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "data" / "daily_output"


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("OK:", msg)


def main() -> None:
    _assert(is_competition_capital(500000), "500k is competition capital")
    _assert(not is_competition_capital(200000), "200k is not competition capital")
    _assert(should_write_competition_artifacts(500000), "500k may write official")
    _assert(not should_write_competition_artifacts(200000), "200k must not write official")

    # Force blocked by default
    os.environ.pop("ETF_CHAT_ALLOW_FORCE_RERUN", None)
    _assert(not chat_force_allowed(), "force env off by default")

    with mock.patch("competition_guard.has_daily_run", return_value=True):
        allowed, reason = guard_chat_prediction_run(force=True)
        _assert(not allowed and reason is not None, "force blocked when run exists")

        allowed2, reason2 = guard_chat_prediction_run(force=False)
        _assert(not allowed2 and reason2 is None, "existing run → use cache, no rerun")

    with mock.patch("competition_guard.has_daily_run", return_value=False):
        allowed3, reason3 = guard_chat_prediction_run(force=False)
        _assert(allowed3 and reason3 is None, "first run of day allowed from chat")

    # Personal advice must not create/modify competition files
    before = {}
    for p in sorted(OUTPUT.glob("*_submit.json"))[-3:]:
        before[p.name] = (p.stat().st_mtime_ns, p.read_bytes())

    advice = build_personal_advice(
        capital=200000,
        risk_preference="aggressive",
        focus="growth",
        allow_latest_fallback=True,
    )
    _assert(advice.get("ok"), "personal advice ok")

    for name, (mtime, content) in before.items():
        p = OUTPUT / name
        _assert(p.exists(), f"{name} still exists")
        _assert(p.stat().st_mtime_ns == mtime, f"{name} mtime unchanged")
        _assert(p.read_bytes() == content, f"{name} content unchanged")

    # save_outputs with non-competition capital goes to personal_output
    from daily_job import save_outputs

    personal_dir = BASE / "data" / "personal_output"
    marker = "isolation-test-do-not-use"
    submit_path, full_path = save_outputs(
        "2099-01-01",
        [{"symbol": "510300", "symbol_name": "沪深300ETF", "volume": 100}],
        {"summary": {"held_stocks": []}, "llm_trace": None},
        {"accepted_articles": []},
        None,
        capital=123456,
    )
    _assert("personal_output" in str(submit_path), f"sandbox path: {submit_path}")
    _assert(submit_path.exists(), "personal submit written")
    # Must not appear under official daily_output
    official = OUTPUT / "2099-01-01_submit.json"
    _assert(not official.exists(), "official submit not created for personal capital")
    # cleanup
    submit_path.unlink(missing_ok=True)
    full_path.unlink(missing_ok=True)

    print("COMPETITION ISOLATION OK")
    print(json.dumps({"competition_capital": COMPETITION_CAPITAL, "advice_holdings": [
        h["symbol"] for h in advice.get("holdings") or []
    ]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
