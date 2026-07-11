"""Decision integrity: price freshness + soft repeat-holding tilt.

Factor priority (high → low). Secondary factors must not overturn a clear
primary signal:

1. Data integrity — stale/incomplete K-lines block LLM rescoring (hard).
2. Primary alpha — composite score (trend + theme + hist − risk).
3. Risk budget — stability overlay / econ caps / score gate (position size).
4. Repeat-holding tilt — consecutive days holding a name soft-lowers its
   preference; never bans; cannot flip a clear score leader.
5. LLM views — only when prices are fresh; may rescore themes, but does not
   lower invest_ratio (hint is audit-only under current defaults).

Concentration used to force 2-names; that over-weighted a secondary concern.
Now it only tilts tendency and optionally trims size slightly.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from features import _get_price_for_decision
from pool import ALL_POOL

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"

ANCHOR_CODES = ("510300", "510880", "512880")

# Soft score tilt for consecutive prior holding days (never a ban).
# days=1 → entering 2nd consecutive day of holding that name.
REPEAT_TILT_BY_DAYS = {1: 1.5, 2: 3.0, 3: 4.0}
REPEAT_TILT_MAX = 4.0
SOLE_STREAK_EXTRA = 0.5  # slight extra if prior days were sole-name
# Only tilt when the race is close; clear leaders are left alone (primary alpha wins).
CLEAR_LEAD_GAP = 4.0


def expected_decision_bar_date(decision_date_str: str) -> date:
    """Last trading day strictly before the decision session (morning run)."""
    from trading_calendar import previous_trading_day

    return previous_trading_day(decision_date_str)


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
    """Audit whether decision-time bars are fresh enough for a real ranking.

    Defaults to ALL_POOL (steady + offensive) so growth ETFs cannot bypass
    freshness checks while still being tradable.
    """
    from market_data import bar_row_looks_incomplete

    codes = codes or [item["code"] for item in ALL_POOL]
    expected = expected_decision_bar_date(decision_date_str)
    per_code: dict[str, dict[str, Any]] = {}
    stale_codes: list[str] = []
    missing_codes: list[str] = []
    incomplete_codes: list[str] = []

    for code in codes:
        last = _last_bar_date(code, decision_date_str)
        if last is None:
            missing_codes.append(code)
            stale_codes.append(code)
            per_code[code] = {
                "last_bar": None, "lag_days": None, "ok": False, "incomplete": False,
            }
            continue
        lag = (expected - last).days
        ok = lag <= 0
        incomplete = False
        # 仅记录「平收+宽振幅」启发式命中，不单独触发 price_stale：
        # Baostock 与本地常一致，属真实平收日，误伤会压仓/挡 LLM。
        df = _get_price_for_decision(code, decision_date_str)
        if df is not None and not df.empty:
            try:
                incomplete = bool(bar_row_looks_incomplete(df.iloc[-1]))
            except Exception:
                incomplete = False
        if incomplete:
            incomplete_codes.append(code)
        per_code[code] = {
            "last_bar": str(last),
            "lag_days": lag,
            "ok": ok,
            "incomplete": incomplete,
        }
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
        "incomplete_codes": incomplete_codes,
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


def compute_holding_streaks(history: list[dict[str, Any]]) -> dict[str, int]:
    """Consecutive prior days each symbol appeared in the portfolio (any size)."""
    if not history:
        return {}
    streaks: dict[str, int] = {}
    newest = history[-1].get("symbols") or []
    for sym in newest:
        code = str(sym).zfill(6)
        days = 0
        for row in reversed(history):
            held = {str(s).zfill(6) for s in (row.get("symbols") or [])}
            if code in held:
                days += 1
            else:
                break
        if days > 0:
            streaks[code] = days
    return streaks


def _tilt_points(hold_days: int, *, sole_extra: bool) -> float:
    if hold_days <= 0:
        return 0.0
    base = REPEAT_TILT_BY_DAYS.get(hold_days, REPEAT_TILT_MAX)
    if hold_days > 3:
        base = REPEAT_TILT_MAX
    if sole_extra:
        base = min(REPEAT_TILT_MAX + SOLE_STREAK_EXTRA, base + SOLE_STREAK_EXTRA)
    return float(base)


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
    holding = compute_holding_streaks(history)
    return {
        "price_audit": price_audit,
        "price_stale": price_audit["price_stale"],
        "block_llm_rescore": price_audit["price_stale"],
        "recent_submit_history": history,
        "sole_symbol_streak": streak,
        "holding_streaks": holding,
    }


def apply_concentration_risk(
    ranked: list[dict[str, Any]],
    invest_ratio: float,
    max_positions: int,
    integrity_ctx: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], float, int, dict[str, Any]]:
    """Soft repeat-holding tilt when the race is close.

    Default OFF: 89-day backtest (2026-03→07) showed stable-only beat
    soft-tilt on total return / Sharpe with similar MDD. Enable with
    ``ETF_REPEAT_TILT=1`` for near-tie preference only.

    - Lowers preference for names held on consecutive prior days.
    - Does NOT ban; does NOT force a second position; does NOT trim size.
    - If #1 leads by ≥ CLEAR_LEAD_GAP, skip entirely (primary alpha wins).
    """
    import os

    audit: dict[str, Any] = {
        "applied": False,
        "mode": "soft_tilt_near_tie",
        "notes": [],
        "tilts": [],
        "original_ratio": round(float(invest_ratio), 4),
        "original_max_positions": int(max_positions),
    }
    if not ranked or not integrity_ctx or invest_ratio <= 0:
        return ranked, invest_ratio, max_positions, audit

    enabled = os.environ.get("ETF_REPEAT_TILT", "").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        audit["mode"] = "disabled"
        audit["notes"] = ["连持倾斜默认关闭（回测显示稳健层更利于稳中盈利；设 ETF_REPEAT_TILT=1 开启）"]
        return ranked, invest_ratio, max_positions, audit

    holding = dict(integrity_ctx.get("holding_streaks") or {})
    sole = integrity_ctx.get("sole_symbol_streak") or {}
    sole_sym = str(sole.get("symbol") or "").zfill(6) if sole else ""
    sole_days = int(sole.get("days") or 0) if sole else 0

    if not holding and sole_days <= 0:
        return ranked, invest_ratio, max_positions, audit

    pre = [(str(x.get("code") or "").zfill(6), float(x.get("score") or 0.0)) for x in ranked]
    lead_code, lead_score = pre[0]
    second_score = pre[1][1] if len(pre) > 1 else lead_score - 99.0
    clear_gap = lead_score - second_score

    # Clear primary lead → do not let a secondary factor touch ranking or size.
    if clear_gap >= CLEAR_LEAD_GAP:
        audit["notes"] = [
            f"第一名领先{clear_gap:.1f}分≥{CLEAR_LEAD_GAP:.0f}，主信号优先，跳过连持倾斜"
        ]
        audit["clear_gap_before"] = round(clear_gap, 2)
        audit["skipped_clear_lead"] = True
        return ranked, invest_ratio, max_positions, audit

    notes: list[str] = [
        f"分差仅{clear_gap:.1f}<{CLEAR_LEAD_GAP:.0f}，近并列时软性下调连持标的倾向"
    ]
    tilts: list[dict[str, Any]] = []
    any_tilt = False

    for item in ranked:
        code = str(item.get("code") or "").zfill(6)
        hold_days = int(holding.get(code) or 0)
        if hold_days <= 0 and not (sole_sym == code and sole_days > 0):
            continue
        if hold_days <= 0:
            hold_days = sole_days
        sole_extra = bool(sole_sym == code and sole_days >= 1)
        pen = _tilt_points(hold_days, sole_extra=sole_extra)
        if pen <= 0:
            continue

        old = float(item.get("score") or 0.0)
        new = round(old - pen, 2)
        item["score"] = new
        item["repeat_hold_days"] = hold_days
        item["repeat_tilt"] = round(-pen, 2)
        any_tilt = True
        tilts.append({
            "code": code,
            "hold_days": hold_days,
            "sole_extra": sole_extra,
            "penalty": round(pen, 2),
            "score_before": old,
            "score_after": new,
        })
        notes.append(
            f"{code}连续持有{hold_days}日，倾向-{pen:.1f}分"
            + ("（单仓连击加权）" if sole_extra else "")
        )

    if not any_tilt:
        return ranked, invest_ratio, max_positions, audit

    ranked = sorted(ranked, key=lambda x: float(x.get("score") or 0.0), reverse=True)
    notes.append("已按综合分重排（连持为次级；不禁买、不强制分散、不单独砍仓）")

    audit["applied"] = True
    audit["notes"] = notes
    audit["tilts"] = tilts
    audit["sole_symbol"] = sole_sym or None
    audit["sole_days"] = sole_days
    audit["holding_streaks"] = holding
    audit["clear_gap_before"] = round(clear_gap, 2)
    audit["final_ratio"] = round(float(invest_ratio), 4)
    audit["final_max_positions"] = int(max_positions)
    audit["final_top"] = str(ranked[0].get("code") or "").zfill(6)

    return ranked, invest_ratio, max_positions, audit


def summarize_integrity_warnings(integrity_ctx: dict[str, Any]) -> list[str]:
    import os

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

    repeat_tilt_enabled = os.environ.get("ETF_REPEAT_TILT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    holding = integrity_ctx.get("holding_streaks") or {}
    sole = integrity_ctx.get("sole_symbol_streak")
    if holding:
        top_hold = max(holding.items(), key=lambda kv: kv[1])
        if top_hold[1] >= 1:
            if repeat_tilt_enabled:
                warnings.append(
                    f"近{top_hold[1]}日连续持有{top_hold[0]}，"
                    f"将软性降低其投资倾向（不禁止买入；明显领先时不翻盘）"
                )
            else:
                warnings.append(
                    f"近{top_hold[1]}日连续持有{top_hold[0]}，"
                    f"已记录集中度风险；重复持仓软倾斜当前未启用"
                )
    elif sole and int(sole.get("days") or 0) >= 1:
        if repeat_tilt_enabled:
            warnings.append(
                f"近{sole['days']}日连续单仓{sole['symbol']}，"
                f"将软性降低其投资倾向（不禁止、不强制分散）"
            )
        else:
            warnings.append(
                f"近{sole['days']}日连续单仓{sole['symbol']}，"
                f"已记录集中度风险；重复持仓软倾斜当前未启用"
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
