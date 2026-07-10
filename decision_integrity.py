"""Decision integrity: price freshness + profit-oriented concentration risk.

Goals:
1. Stale K-lines must not freeze rankings / LLM rubber-stamping.
2. Consecutive single-name concentration is a drawdown risk — diversify
   (keep #1, add #2) rather than ban repeating the same ETF.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from features import _get_price_for_decision
from pool import TRADING_POOL

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"

ANCHOR_CODES = ("510300", "510880", "512880")

# After N consecutive sole-name days of the same ETF, force diversification.
SOLE_STREAK_FORCE_2_DAYS = 2
SOLE_STREAK_HARD_CAP_DAYS = 3
SOLE_STREAK_RATIO_CAP_2 = 0.35   # day 2+: tighten invest ratio
SOLE_STREAK_RATIO_CAP_3 = 0.25   # day 3+: tighter still


def expected_decision_bar_date(decision_date_str: str) -> date:
    """Last trading day strictly before the decision session (morning run)."""
    d = pd.to_datetime(decision_date_str).date()
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _last_bar_date(code: str, decision_date_str: str) -> date | None:
    df = _get_price_for_decision(code, decision_date_str)
    if df is None or df.empty:
        return None
    last = pd.to_datetime(df["date"].iloc[-1], errors="coerce")
    return last.date() if pd.notna(last) else None


def audit_price_freshness(
    decision_date_str: str,
    codes: list[str] | None = None,
    *,
    price_update_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit whether decision-time bars are fresh enough for a real ranking."""
    codes = codes or [item["code"] for item in TRADING_POOL]
    expected = expected_decision_bar_date(decision_date_str)
    per_code: dict[str, dict[str, Any]] = {}
    stale_codes: list[str] = []
    missing_codes: list[str] = []

    for code in codes:
        last = _last_bar_date(code, decision_date_str)
        if last is None:
            missing_codes.append(code)
            per_code[code] = {"last_bar": None, "lag_days": None, "ok": False}
            continue
        lag = (expected - last).days
        ok = lag <= 0
        per_code[code] = {"last_bar": str(last), "lag_days": lag, "ok": ok}
        if not ok:
            stale_codes.append(code)

    anchor_stale = [
        c for c in ANCHOR_CODES
        if c in per_code and not per_code[c].get("ok")
    ]
    stale_ratio = len(stale_codes) / max(1, len(codes))

    if price_update_stats:
        degraded = int(price_update_stats.get("degraded", 0))
        total = int(price_update_stats.get("ok", len(codes)))
        degraded_ratio = degraded / max(1, total)
    else:
        degraded_ratio = 0.0

    price_stale = bool(anchor_stale) or stale_ratio >= 0.5 or degraded_ratio >= 0.5

    return {
        "decision_date": decision_date_str,
        "expected_bar_date": str(expected),
        "per_code": per_code,
        "stale_codes": stale_codes,
        "missing_codes": missing_codes,
        "anchor_stale": anchor_stale,
        "stale_ratio": round(stale_ratio, 3),
        "price_stale": price_stale,
        "degraded_fetch_ratio": round(degraded_ratio, 3),
        "price_update_stats": price_update_stats or {},
    }


def load_recent_submit_history(
    decision_date_str: str,
    lookback: int = 6,
) -> list[dict[str, Any]]:
    """Prior competition submits with date < decision_date, oldest→newest."""
    cutoff = pd.to_datetime(decision_date_str).date()
    hits: list[tuple[date, list[str]]] = []
    if not OUTPUT_DIR.exists():
        return []

    for path in OUTPUT_DIR.glob("*_submit.json"):
        try:
            d = pd.to_datetime(path.name.split("_")[0]).date()
        except ValueError:
            continue
        if d >= cutoff:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        symbols = [str(x.get("symbol", "")).zfill(6) for x in data if x.get("symbol")]
        hits.append((d, symbols))

    hits.sort(key=lambda x: x[0])
    return [{"date": str(d), "symbols": syms} for d, syms in hits[-lookback:]]


def compute_sole_symbol_streak(history: list[dict[str, Any]]) -> dict[str, Any] | None:
    """If recent days all held exactly one symbol, return {symbol, days}."""
    if not history:
        return None
    streak_sym: str | None = None
    streak_days = 0
    for row in reversed(history):
        syms = row.get("symbols") or []
        if len(syms) != 1:
            break
        sym = syms[0]
        if streak_sym is None:
            streak_sym = sym
            streak_days = 1
        elif sym == streak_sym:
            streak_days += 1
        else:
            break
    if streak_sym and streak_days >= 1:
        return {"symbol": streak_sym, "days": streak_days}
    return None


def build_integrity_context(
    decision_date_str: str,
    *,
    price_update_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    price_audit = audit_price_freshness(
        decision_date_str,
        price_update_stats=price_update_stats,
    )
    history = load_recent_submit_history(decision_date_str)
    streak = compute_sole_symbol_streak(history)
    return {
        "price_audit": price_audit,
        "price_stale": price_audit["price_stale"],
        "block_llm_rescore": price_audit["price_stale"],
        "recent_submit_history": history,
        "sole_symbol_streak": streak,
    }


def apply_concentration_risk(
    ranked: list[dict[str, Any]],
    invest_ratio: float,
    max_positions: int,
    integrity_ctx: dict[str, Any] | None,
) -> tuple[float, int, dict[str, Any]]:
    """Profit guard against multi-day single-name concentration.

    Does NOT ban the top ETF. If the same sole name was held for N prior days
    and would be sole again, force at least 2 names (keep #1, add #2) and
    tighten invest_ratio — concentration is what blows up drawdowns.
    """
    audit: dict[str, Any] = {
        "applied": False,
        "notes": [],
        "original_ratio": round(float(invest_ratio), 4),
        "original_max_positions": int(max_positions),
    }
    if not ranked or not integrity_ctx or invest_ratio <= 0:
        return invest_ratio, max_positions, audit

    streak = integrity_ctx.get("sole_symbol_streak")
    if not streak:
        return invest_ratio, max_positions, audit

    sym = str(streak.get("symbol") or "").zfill(6)
    days = int(streak.get("days") or 0)
    top = str(ranked[0].get("code") or "").zfill(6)
    price_stale = bool(integrity_ctx.get("price_stale"))

    # Only act when we are about to concentrate again on the same sole name.
    if days < SOLE_STREAK_FORCE_2_DAYS or top != sym:
        return invest_ratio, max_positions, audit

    new_ratio = float(invest_ratio)
    new_max = int(max_positions)
    notes: list[str] = []

    # Day 2+ of same sole name → require 2 positions if a second candidate exists.
    if days >= SOLE_STREAK_FORCE_2_DAYS and len(ranked) >= 2:
        if new_max < 2:
            new_max = 2
            notes.append(
                f"连续{days}日单仓{sym}，强制分散至2仓（保留第一名，加入第二名）"
            )
        new_ratio = min(new_ratio, SOLE_STREAK_RATIO_CAP_2)
        notes.append(f"集中度风控仓位上限{SOLE_STREAK_RATIO_CAP_2:.0%}")

    # Day 3+ → tighter ratio; still keep #1 but must diversify.
    if days >= SOLE_STREAK_HARD_CAP_DAYS and len(ranked) >= 2:
        new_max = max(new_max, 2)
        new_ratio = min(new_ratio, SOLE_STREAK_RATIO_CAP_3)
        notes.append(
            f"连续{days}日单仓{sym}已达硬顶，仓位上限{SOLE_STREAK_RATIO_CAP_3:.0%}且至少2仓"
        )

    # Stale prices + concentration → even more dangerous (same fake ranking).
    if price_stale and days >= SOLE_STREAK_FORCE_2_DAYS and len(ranked) >= 2:
        new_max = max(new_max, 2)
        new_ratio = min(new_ratio, SOLE_STREAK_RATIO_CAP_3)
        notes.append("行情陈旧叠加连续单仓，强制2仓并收紧至25%")

    if new_ratio < invest_ratio or new_max != max_positions:
        audit["applied"] = True
        audit["notes"] = notes
        audit["sole_symbol"] = sym
        audit["sole_days"] = days
        audit["final_ratio"] = round(new_ratio, 4)
        audit["final_max_positions"] = new_max

    return new_ratio, new_max, audit


def summarize_integrity_warnings(integrity_ctx: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not integrity_ctx:
        return warnings
    pa = integrity_ctx.get("price_audit") or {}
    if pa.get("price_stale"):
        warnings.append(
            f"行情数据陈旧（期望K线至{pa.get('expected_bar_date')}，"
            f"陈旧比例{pa.get('stale_ratio', 0):.0%}），"
            f"已禁用LLM主题重排并收紧仓位上限"
        )
    streak = integrity_ctx.get("sole_symbol_streak")
    if streak and int(streak.get("days") or 0) >= SOLE_STREAK_FORCE_2_DAYS:
        warnings.append(
            f"近{streak['days']}个交易日连续单仓{streak['symbol']}，"
            f"将启用集中度风控（强制分散/降仓，不禁止持有该ETF）"
        )
    return warnings


def apply_integrity_env_caps(integrity_ctx: dict[str, Any]) -> None:
    """Tighten position cap when prices are stale (non-blocking)."""
    import os

    if not integrity_ctx or not integrity_ctx.get("price_stale"):
        return
    existing = os.environ.get("FORCE_POSITION_CAP", "").strip()
    try:
        cur = float(existing) if existing else 1.0
    except ValueError:
        cur = 1.0
    os.environ["FORCE_POSITION_CAP"] = str(min(cur, 0.30))
