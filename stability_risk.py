"""Short-race stability controls for a configurable competition window.

The main strategy is allowed to find opportunities, but this layer keeps the
race account from leaning too hard after recent losses or weak signal quality.
It is intentionally small and auditable so experiments on this branch can be
compared against the original strategy.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from settlement_prices import get_close_to_close

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "daily_output"


def stable_mode_enabled() -> bool:
    """Return whether the conservative recent-performance overlay is active."""
    return os.environ.get("ETF_AGENT_STABLE_MODE", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _settle_competition_output(
    competition_output: list[dict[str, Any]],
    trade_date: str,
    *,
    data_dir: Path = DATA_DIR,
) -> float | None:
    total = 0.0
    for item in competition_output:
        code = str(item.get("symbol") or "").zfill(6)
        volume = int(float(item.get("volume") or 0))
        prices = get_close_to_close(code, trade_date, data_dir=data_dir)
        if not code or volume <= 0 or prices is None:
            return None
        prev_close, today_close = prices
        total += volume * (today_close - prev_close)
    return float(total)


def build_recent_risk_context(
    as_of: str,
    *,
    capital: float,
    output_dir: Path = OUTPUT_DIR,
    data_dir: Path = DATA_DIR,
    lookback: int = 5,
) -> dict[str, Any]:
    """Summarize settled PnL from previous generated predictions before ``as_of``."""
    context: dict[str, Any] = {
        "enabled": stable_mode_enabled(),
        "as_of": as_of,
        "lookback": lookback,
        "rows": [],
        "last_pnl": 0.0,
        "last5_pnl": 0.0,
        "last5_return_pct": 0.0,
        "consecutive_losses": 0,
        "win_rate": 0.0,
    }
    if not context["enabled"] or not output_dir.exists():
        return context

    cutoff = pd.to_datetime(as_of, errors="coerce")
    if pd.isna(cutoff):
        return context

    rows: list[dict[str, Any]] = []
    for path in sorted(output_dir.glob("*_full.json")):
        try:
            trade_date = path.name.split("_")[0]
            d = pd.to_datetime(trade_date, errors="coerce")
            if pd.isna(d) or d >= cutoff:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("mode") in {"personal_sandbox", "fatal_fallback"}:
                continue
            comp = payload.get("competition_output") or []
            pnl = _settle_competition_output(comp, trade_date, data_dir=data_dir)
            if pnl is None:
                continue
            rows.append({
                "date": trade_date,
                "pnl": round(pnl, 2),
                "positions": len(comp),
                "source_file": str(path),
            })
        except Exception:
            continue

    rows = rows[-lookback:]
    if not rows:
        return context

    consecutive_losses = 0
    for row in reversed(rows):
        if float(row["pnl"]) < 0:
            consecutive_losses += 1
        else:
            break

    total = sum(float(row["pnl"]) for row in rows)
    wins = sum(1 for row in rows if float(row["pnl"]) > 0)
    context.update({
        "rows": rows,
        "last_pnl": float(rows[-1]["pnl"]),
        "last5_pnl": round(total, 2),
        "last5_return_pct": round(total / capital * 100, 3) if capital else 0.0,
        "consecutive_losses": consecutive_losses,
        "win_rate": round(wins / len(rows), 3),
    })
    return context


def summarize_risk_context(context: dict[str, Any]) -> str:
    if not context.get("enabled"):
        return "稳健模式关闭"
    rows = context.get("rows") or []
    if not rows:
        return "无可复盘历史，采用稳健默认上限"
    return (
        f"近{len(rows)}次PnL={context.get('last5_pnl', 0):+.2f}元，"
        f"上一日={context.get('last_pnl', 0):+.2f}元，"
        f"连续亏损={context.get('consecutive_losses', 0)}"
    )

