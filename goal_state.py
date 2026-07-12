"""Goal-aware risk controls for a ten-trading-day ETF competition window."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd

from settlement_prices import get_close_to_close

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "daily_output"
GOAL_STATE_PATH = DATA_DIR / "goal_window.json"

GOAL_WINDOW_DAYS = 10
GOAL_TARGET_RETURN = 0.005
GOAL_PROTECT_RETURN = 0.0035
GOAL_MAX_DRAWDOWN = -0.010
GOAL_PROTECT_CAP = 0.15
GOAL_DRAWDOWN_CAP = 0.15
GOAL_LATE_WINDOW_CAP = 0.35
DAILY_VAR_BUDGET = 0.005
DAILY_VAR_Z = 1.65
VOLATILITY_CAP_MAX = 0.40
VOLATILITY_CAP_MIN = 0.15


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _settle_output(items: list[dict[str, Any]], trade_date: str, data_dir: Path) -> float:
    pnl = 0.0
    for item in items:
        code = str(item.get("symbol") or "").zfill(6)
        volume = int(float(item.get("volume") or 0))
        prices = get_close_to_close(code, trade_date, data_dir=data_dir)
        if prices is None or volume <= 0:
            continue
        prev_close, today_close = prices
        pnl += volume * (today_close - prev_close)
    return float(pnl)


def summarize_goal_rows(
    as_of: str,
    *,
    capital: float,
    rows: list[dict[str, Any]],
    window_days: int = GOAL_WINDOW_DAYS,
    start_date: str | None = None,
    fixed_window: bool = False,
) -> dict[str, Any]:
    target = _env_float("ETF_GOAL_TARGET_RETURN", GOAL_TARGET_RETURN)
    protect = _env_float("ETF_GOAL_PROTECT_RETURN", GOAL_PROTECT_RETURN)
    max_drawdown = _env_float("ETF_GOAL_MAX_DRAWDOWN", GOAL_MAX_DRAWDOWN)
    rows = list(rows)
    rows = rows[:window_days] if fixed_window else rows[-max(0, window_days - 1) :]
    total_pnl = sum(float(row.get("pnl") or 0.0) for row in rows)
    cumulative_return = total_pnl / capital if capital else 0.0
    days_elapsed = len(rows)
    days_remaining = max(1, window_days - days_elapsed)

    if fixed_window and days_elapsed >= window_days:
        status = "window_complete"
    elif cumulative_return >= target:
        status = "target_achieved"
    elif cumulative_return >= protect:
        status = "protect_profit"
    elif cumulative_return <= max_drawdown:
        status = "drawdown_defense"
    else:
        status = "active"

    return {
        "enabled": True,
        "as_of": as_of,
        "window_days": int(window_days),
        "start_date": start_date,
        "window_mode": "fixed" if fixed_window else "rolling_monitor",
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "target_return": round(target, 6),
        "protect_return": round(protect, 6),
        "max_drawdown": round(max_drawdown, 6),
        "cumulative_pnl": round(total_pnl, 2),
        "cumulative_return": round(cumulative_return, 8),
        "remaining_return": round(max(0.0, target - cumulative_return), 8),
        "status": status,
        "rows": rows,
    }


def build_goal_state(
    as_of: str,
    *,
    capital: float,
    output_dir: Path = OUTPUT_DIR,
    data_dir: Path = DATA_DIR,
    window_days: int = GOAL_WINDOW_DAYS,
    state_path: Path = GOAL_STATE_PATH,
) -> dict[str, Any]:
    """Build the state from immutable, already-settled official predictions.

    ``ETF_GOAL_START_DATE`` may pin a competition start date. Without it, the
    previous ``window_days - 1`` settled predictions form a rolling window.
    """
    cutoff = pd.to_datetime(as_of, errors="coerce")
    start_raw = os.environ.get("ETF_GOAL_START_DATE", "").strip()
    if not start_raw:
        try:
            import json

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            start_raw = str(saved.get("start_date") or "").strip()
        except Exception:
            start_raw = ""
    if not start_raw:
        start_raw = as_of
        try:
            import json

            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {"start_date": start_raw, "window_days": int(window_days)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
    start = pd.to_datetime(start_raw, errors="coerce") if start_raw else pd.NaT

    rows: list[dict[str, Any]] = []
    if output_dir.exists() and pd.notna(cutoff):
        for path in sorted(output_dir.glob("*_full.json")):
            try:
                import json

                trade_date = path.name[:10]
                date = pd.to_datetime(trade_date, errors="coerce")
                if pd.isna(date) or date >= cutoff or (pd.notna(start) and date < start):
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("mode") in {"personal_sandbox", "fatal_fallback"}:
                    continue
                items = payload.get("competition_output") or []
                pnl = _settle_output(items, trade_date, data_dir)
                rows.append(
                    {
                        "date": trade_date,
                        "pnl": round(pnl, 2),
                        "return": round(pnl / capital, 8) if capital else 0.0,
                        "positions": len(items),
                    }
                )
            except Exception:
                continue

    return summarize_goal_rows(
        as_of,
        capital=capital,
        rows=rows,
        window_days=window_days,
        start_date=start_raw or None,
        fixed_window=True,
    )


def apply_goal_overlay(
    invest_ratio: float,
    max_positions: int,
    ranked: list[dict[str, Any]],
    goal_state: dict[str, Any] | None,
) -> tuple[float, int, dict[str, Any] | None]:
    """Reduce exposure according to goal progress and prospective volatility.

    The overlay never increases exposure. Volatility sizing uses a simple
    one-day 95% loss budget and is deliberately auditable rather than fitted.
    """
    if not goal_state or not goal_state.get("enabled"):
        return float(invest_ratio), int(max_positions), None

    original_ratio = float(invest_ratio)
    original_positions = int(max_positions)
    control_mode = os.environ.get("ETF_TEN_DAY_GOAL_MODE", "monitor").strip().lower()
    if control_mode == "off":
        return original_ratio, original_positions, None
    cap = 1.0
    position_cap = original_positions
    notes: list[str] = []
    status = str(goal_state.get("status") or "active")

    enforce_goal = control_mode in {"fixed", "enforce", "on"}
    if enforce_goal and status == "target_achieved":
        cap = 0.0
        position_cap = 1
        notes.append("ten-day target reached; lock profit in cash")
    elif enforce_goal and status == "window_complete":
        cap = 0.0
        position_cap = 1
        notes.append("ten-day window complete; freeze further risk")
    elif enforce_goal and status == "protect_profit":
        cap = min(cap, _env_float("ETF_GOAL_PROTECT_CAP", GOAL_PROTECT_CAP))
        position_cap = 1
        notes.append("near ten-day target; protect accumulated profit")
    elif enforce_goal and status == "drawdown_defense":
        cap = min(cap, _env_float("ETF_GOAL_DRAWDOWN_CAP", GOAL_DRAWDOWN_CAP))
        position_cap = 1
        notes.append("ten-day drawdown budget exhausted; defensive exposure")
    elif enforce_goal and int(goal_state.get("days_remaining") or GOAL_WINDOW_DAYS) <= 3:
        cap = min(cap, _env_float("ETF_GOAL_LATE_WINDOW_CAP", GOAL_LATE_WINDOW_CAP))
        notes.append("late in goal window; no catch-up leverage")

    volatilities = [
        float(item.get("volatility_20d_pct") or 0.0)
        for item in ranked[: max(1, position_cap)]
        if float(item.get("volatility_20d_pct") or 0.0) > 0
    ]
    volatility_cap = None
    if volatilities:
        reference_vol = max(volatilities) / 100.0
        budget = _env_float("ETF_DAILY_VAR_BUDGET", DAILY_VAR_BUDGET)
        raw_cap = budget / max(DAILY_VAR_Z * reference_vol, 1e-9)
        volatility_cap = max(VOLATILITY_CAP_MIN, min(VOLATILITY_CAP_MAX, raw_cap))
        cap = min(cap, volatility_cap)
        notes.append(
            f"95% one-day risk budget caps exposure at {volatility_cap:.0%} "
            f"for {reference_vol:.2%} volatility"
        )

    final_ratio = min(original_ratio, cap)
    final_positions = max(1, min(original_positions, position_cap))
    audit = {
        "enabled": True,
        "control_mode": control_mode,
        "status": status,
        "original_invest_ratio": round(original_ratio, 4),
        "final_invest_ratio": round(final_ratio, 4),
        "original_max_positions": original_positions,
        "final_max_positions": final_positions,
        "goal_cap": round(cap, 4),
        "volatility_cap": round(volatility_cap, 4) if volatility_cap is not None else None,
        "notes": notes,
        "goal_state": goal_state,
    }
    return final_ratio, final_positions, audit
