"""Live personal advice runner.

Uses base data only (K-line CSVs, news signal, econ calendar), then runs
strategy + optional LLM **now** for the user's capital / risk / focus.

Never writes competition artifacts (data/daily_output, data/agent_kb).
Optional sandbox write goes to data/personal_output only.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from competition_guard import COMPETITION_CAPITAL, personal_output_paths
from daily_job import (
    build_daily_news_signal,
    build_llm_decision,
    to_competition_output,
)
from econ_calendar import load_econ_payload
from info_collector import DISCLAIMER, FOCUS_LABELS, RISK_LABELS
from personalized_advisor import (
    RISK_PARAMS,
    _select_and_allocate,
    risk_position_note,
)
from stability_risk import build_recent_risk_context
from strategy import run_decision
from theme_signal import signal_path


BASE_DIR = Path(__file__).resolve().parent
NEWS_DIR = BASE_DIR / "data" / "daily_news_signal"


def _log(msg: str) -> None:
    print(f"[live_personal] {msg}")


def _load_news_base(date_str: str, *, allow_fetch: bool = True) -> dict[str, Any]:
    """Prefer on-disk news signal (base data). Optionally fetch if missing."""
    path = signal_path(date_str)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                _log(f"news base loaded: {path.name}")
                return data
        except Exception as exc:
            _log(f"news base read failed: {exc}")
    if not allow_fetch:
        return {}
    _log("news base missing → building from sources (writes news signal only)")
    try:
        return build_daily_news_signal(date_str, "09:30")
    except Exception as exc:
        _log(f"news build failed: {exc}")
        return {}


def _apply_risk_env(risk: str) -> str | None:
    """Temporarily tighten FORCE_POSITION_CAP for conservative users. Returns previous value."""
    prev = os.environ.get("FORCE_POSITION_CAP")
    params = RISK_PARAMS.get(risk) or RISK_PARAMS["balanced"]
    cap = float(params["invest_cap"])
    # Only tighten; never raise an existing stricter cap
    try:
        existing = float(prev) if prev not in (None, "") else 1.0
    except ValueError:
        existing = 1.0
    os.environ["FORCE_POSITION_CAP"] = str(min(cap, existing))
    return prev


def _restore_risk_env(prev: str | None) -> None:
    if prev is None:
        os.environ.pop("FORCE_POSITION_CAP", None)
    else:
        os.environ["FORCE_POSITION_CAP"] = prev


def _save_personal_sandbox(
    date_str: str,
    advice: dict[str, Any],
    strategy_result: dict[str, Any],
    news_signal: dict[str, Any],
    econ_payload: dict[str, Any],
) -> Path | None:
    try:
        paths = personal_output_paths(date_str)
        payload = {
            "date": date_str,
            "mode": "personal_live",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "advice": advice,
            "strategy_result": {
                "summary": strategy_result.get("summary"),
                "market_reason": strategy_result.get("market_reason"),
                "llm_trace": strategy_result.get("llm_trace"),
                "ranked": (strategy_result.get("ranked") or [])[:10],
            },
            "news_stats": {
                "accepted_count": news_signal.get("accepted_count"),
                "confidence": news_signal.get("confidence"),
            },
            "econ_event_count": econ_payload.get("event_count"),
        }
        paths["full"].write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        holdings = [
            {"symbol": h["symbol"], "symbol_name": h.get("symbol_name"), "volume": h.get("volume")}
            for h in (advice.get("holdings") or [])
        ]
        paths["submit"].write_text(json.dumps(holdings, ensure_ascii=False, indent=2), encoding="utf-8")
        return paths["full"]
    except Exception as exc:
        _log(f"sandbox save skipped: {exc}")
        return None


def run_live_personal_advice(
    *,
    capital: float,
    risk_preference: str = "balanced",
    focus: str = "auto",
    prefer_codes: list[str] | None = None,
    avoid_codes: list[str] | None = None,
    date_str: str | None = None,
    allow_news_fetch: bool = True,
    use_llm: bool = True,
    save_sandbox: bool = True,
) -> dict[str, Any]:
    """
    Live path:
      base data (K-line / news / econ) → run_decision(now) → personalize → advice

    Competition isolation:
      - capital is the user's capital (≠ 500k typically)
      - never writes data/daily_output or agent_kb
      - optional write only under data/personal_output
    """
    date_str = (date_str or datetime.now().strftime("%Y-%m-%d"))[:10]
    risk = risk_preference if risk_preference in RISK_PARAMS else "balanced"
    focus = focus if focus in FOCUS_LABELS else "auto"
    prefer_codes = [str(c).zfill(6) for c in (prefer_codes or [])]
    avoid_codes = [str(c).zfill(6) for c in (avoid_codes or [])]
    capital = float(capital)

    if datetime.strptime(date_str, "%Y-%m-%d").weekday() >= 5:
        return {
            "ok": False,
            "live": True,
            "date": date_str,
            "error": "今日 A 股休市，无法现算投资建议。",
        }

    # Guard: never pretend this is a competition run
    if abs(capital - COMPETITION_CAPITAL) < 1:
        _log("user capital equals competition capital; still writing personal sandbox only")

    news_signal = _load_news_base(date_str, allow_fetch=allow_news_fetch)
    econ_payload = load_econ_payload(date_str, allow_live=True, refresh=False)
    recent_risk = build_recent_risk_context(date_str, capital=capital)

    prev_cap = _apply_risk_env(risk)
    llm_payload = None
    llm_decision = None
    try:
        if use_llm:
            llm_payload = build_llm_decision(date_str, capital, news_signal, econ_payload)
            llm_decision = (llm_payload or {}).get("decision")

        _log(f"running strategy live capital={capital:.0f} risk={risk} focus={focus}")
        result = run_decision(
            date_str,
            capital,
            llm_decision=llm_decision,
            econ_payload=econ_payload,
            recent_risk=recent_risk,
        )
    except Exception as exc:
        _restore_risk_env(prev_cap)
        return {
            "ok": False,
            "live": True,
            "date": date_str,
            "error": f"现算失败：{exc}",
            "traceback": traceback.format_exc()[-500:],
        }
    finally:
        _restore_risk_env(prev_cap)

    ranked = list(result.get("ranked") or [])
    summary = result.get("summary") or {}
    base_ratio = float(summary.get("invest_ratio") or 0.0)
    llm_trace = result.get("llm_trace") or {}
    decision_summary = (
        (llm_trace.get("summary_zh") if isinstance(llm_trace, dict) else None)
        or result.get("market_reason")
        or "基于当日基础数据现算。"
    )

    holdings: list[dict[str, Any]] = []
    alloc_meta: dict[str, Any] = {"mode": "live_run"}

    if ranked:
        holdings, alloc_meta = _select_and_allocate(
            ranked,
            capital=capital,
            risk=risk,
            focus=focus,
            prefer_codes=prefer_codes,
            avoid_codes=avoid_codes,
            base_invest_ratio=base_ratio if base_ratio > 0 else float((RISK_PARAMS[risk]["invest_cap"])),
            date_str=date_str,
        )
        alloc_meta["mode"] = "live_run_personalized"
        alloc_meta["strategy_invest_ratio"] = base_ratio
    else:
        # Strategy returned empty ranked — use competition-format from this live run
        raw = to_competition_output(result)
        for h in raw:
            holdings.append({
                "symbol": h["symbol"],
                "symbol_name": h.get("symbol_name"),
                "volume": h.get("volume"),
                "reason": decision_summary,
            })
        alloc_meta = {"mode": "live_run_raw", "strategy_invest_ratio": base_ratio}

    # Attach news titles from base news signal
    accepted = list(news_signal.get("accepted_articles") or [])[:40]
    for h in holdings:
        code = h["symbol"]
        related = []
        for art in accepted:
            themes = art.get("theme_scores") or {}
            if code in themes:
                related.append({
                    "title": art.get("title", ""),
                    "url": art.get("url", ""),
                    "direction": "偏多" if float(themes[code]) >= 0.35 else (
                        "偏空" if float(themes[code]) <= -0.35 else "中性"
                    ),
                })
            if len(related) >= 3:
                break
        h["related_news"] = related

    advice = {
        "ok": True,
        "live": True,
        "need_run": False,
        "date": date_str,
        "used_fallback": False,
        "mode": "personal_live",
        "capital": int(capital),
        "risk_preference": risk,
        "focus": focus,
        "prefer_codes": prefer_codes,
        "avoid_codes": avoid_codes,
        "base_capital": int(COMPETITION_CAPITAL),
        "is_empty": len(holdings) == 0,
        "holdings": holdings,
        "decision_summary_zh": str(decision_summary),
        "market_context_zh": str(result.get("market_reason") or "")[:200],
        "risk_note": risk_position_note(risk, holdings, capital, focus),
        "alloc_meta": alloc_meta,
        "disclaimer": DISCLAIMER,
        "personalization_note": (
            f"已用当日基础数据（行情/新闻/宏观）**现算**策略，"
            f"并按「{RISK_LABELS.get(risk)} / {FOCUS_LABELS.get(focus)}」调整；"
            "结果写入个人沙箱，**不改动**比赛官方预测。"
        ),
        "llm_used": bool(llm_decision),
    }

    if save_sandbox:
        path = _save_personal_sandbox(date_str, advice, result, news_signal, econ_payload)
        if path:
            advice["sandbox_path"] = str(path)

    return advice


# Re-export for callers that only need the live entrypoint
__all__ = [
    "run_live_personal_advice",
]
