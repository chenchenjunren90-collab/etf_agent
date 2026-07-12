"""Smoke test for conversational agent modules (no server required)."""

from __future__ import annotations

import json
import agent_orchestrator as orchestrator
from agent_orchestrator import handle_chat, start_session
from info_collector import parse_capital, wants_personal_advice


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print("OK:", msg)


def main() -> None:
    real_is_trading_day = orchestrator.is_trading_day
    orchestrator.is_trading_day = lambda _value: True
    _assert(wants_personal_advice("我想要今日投资建议"), "detect personal advice")
    _assert(parse_capital("20万") == 200000, "parse 20万")
    _assert(parse_capital(150000) == 150000, "parse int capital")

    boot = start_session()
    sid = boot["session"]["session_id"]
    _assert(boot["intent"] == "greeting", "welcome greeting")
    _assert(any(b.get("type") == "choices" for b in boot["ui_blocks"]), "entry choices")

    # stock boundary
    r = handle_chat("茅台股票能买吗", session_id=sid)
    _assert(r["intent"] == "boundary_stock", f"stock boundary, got {r['intent']}")

    # off topic
    r = handle_chat("今天天气怎么样", session_id=sid)
    _assert(r["intent"] == "off_topic", f"off topic, got {r['intent']}")

    # start collection
    r = handle_chat("今日投资建议", session_id=sid)
    _assert(r["intent"] == "collect_info", f"start collect, got {r['intent']}")
    _assert(r["session"]["state"] == "collecting", "state collecting")
    _assert(any(b.get("field") == "capital" for b in r["ui_blocks"]), "capital ui block")

    # answer capital via field_answer
    r = handle_chat("20万", session_id=sid, field_answer={"field": "capital", "value": 200000})
    _assert(r["intent"] == "collect_info", f"next field after capital, got {r['intent']}")
    _assert(r["session"]["profile"].get("capital") == 200000, "capital saved")
    _assert(any(b.get("field") == "risk_preference" for b in r["ui_blocks"]), "risk ui")

    # answer risk — may generate advice (needs kb) or ask to run
    r = handle_chat("均衡", session_id=sid, field_answer={"field": "risk_preference", "value": "balanced"})
    print("after risk:", r["intent"], r["session"]["state"])
    _assert(r["intent"] in ("personal_advice", "no_kb", "run_error", "collect_info"), "advice or fallback")
    if r["intent"] == "personal_advice":
        _assert(any(b.get("type") == "advice_card" for b in r["ui_blocks"]), "advice card present")
        print(r["reply"][:300])

    # competition path (may use existing kb)
    r2 = handle_chat("今日比赛提交格式", session_id=None)
    print("competition:", r2["intent"], "blocks", [b.get("type") for b in r2.get("ui_blocks") or []])
    _assert(r2["intent"] in ("competition", "run_error", "no_kb"), "competition intent")

    # Closed market must be visible immediately and block every advice path.
    orchestrator.is_trading_day = lambda _value: False
    closed_boot = start_session()
    _assert(closed_boot.get("market_closed") is True, "closed status in welcome")
    _assert("暂无当日 ETF 投资建议" in closed_boot["reply"], "closed welcome message")
    closed_options = [
        opt.get("value")
        for block in closed_boot.get("ui_blocks") or []
        for opt in block.get("options") or []
    ]
    _assert("今日投资建议" not in closed_options, "closed welcome hides advice action")
    closed_sid = closed_boot["session"]["session_id"]
    closed_advice = handle_chat("今日投资建议", session_id=closed_sid)
    _assert(closed_advice["intent"] == "market_closed", "closed personal advice blocked")
    closed_comp = handle_chat("今日比赛提交格式", session_id=closed_sid)
    _assert(closed_comp["intent"] == "market_closed", "closed competition blocked")
    _assert(not closed_comp.get("ui_blocks"), "closed response has no holdings")
    orchestrator.is_trading_day = real_is_trading_day

    print("\nALL SMOKE TESTS PASSED")
    print(json.dumps({"sample_session": r.get("session")}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
