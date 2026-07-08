"""Compare the original strategy with the 10-day stability overlay.

This is a local research helper. It never refreshes market data and settles PnL
with the platform rule: previous close buy price -> same-day close sell price.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from daily_job import to_competition_output
from settlement_prices import get_close_to_close
from strategy import reset_rotation_tracker, run_decision

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CAPITAL = 500000.0


def _settle(competition_output: list[dict[str, Any]], trade_date: str) -> tuple[float, float]:
    total = 0.0
    used = 0.0
    for item in competition_output:
        code = str(item.get("symbol") or "").zfill(6)
        volume = int(float(item.get("volume") or 0))
        prices = get_close_to_close(code, trade_date, data_dir=DATA_DIR)
        if not code or volume <= 0 or prices is None:
            continue
        prev_close, today_close = prices
        total += volume * (today_close - prev_close)
        used += volume * prev_close
    return float(total), float(used)


def _risk_context(rows: list[dict[str, Any]], as_of: str, lookback: int = 5) -> dict[str, Any]:
    recent = rows[-lookback:]
    consecutive_losses = 0
    for row in reversed(recent):
        if float(row["pnl"]) < 0:
            consecutive_losses += 1
        else:
            break
    total = sum(float(row["pnl"]) for row in recent)
    wins = sum(1 for row in recent if float(row["pnl"]) > 0)
    return {
        "enabled": True,
        "as_of": as_of,
        "lookback": lookback,
        "rows": [
            {"date": row["date"], "pnl": round(float(row["pnl"]), 2), "positions": row["n"]}
            for row in recent
        ],
        "last_pnl": round(float(recent[-1]["pnl"]), 2) if recent else 0.0,
        "last5_pnl": round(total, 2),
        "last5_return_pct": round(total / CAPITAL * 100, 3),
        "consecutive_losses": consecutive_losses,
        "win_rate": round(wins / len(recent), 3) if recent else 0.0,
    }


def _trade_dates(start: str, end: str) -> list[str]:
    ref = pd.read_csv(DATA_DIR / "510300.csv")
    date_col = ref.columns[0]
    ref[date_col] = pd.to_datetime(ref[date_col], errors="coerce")
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    return [
        d.strftime("%Y-%m-%d")
        for d in ref[date_col].dropna()
        if start_ts <= d <= end_ts
    ]


def _stats(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    returns = df["ret"].astype(float)
    total_pnl = float(df["pnl"].sum())
    std = float(returns.std(ddof=1))
    curve = (1 + returns).cumprod()
    max_drawdown = float(((curve / curve.cummax()) - 1).min()) if len(curve) else 0.0
    return {
        "label": label,
        "days": int(len(df)),
        "total_pnl": round(total_pnl, 2),
        "total_ret_pct": round(total_pnl / CAPITAL * 100, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 1),
        "avg_used_pct": round(float((df["used"] / CAPITAL).mean()) * 100, 1),
        "sharpe_ann": round(float(returns.mean()) / std * math.sqrt(252), 2) if std else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "last10_pnl": round(float(df.tail(10)["pnl"].sum()), 2),
        "last10_ret_pct": round(float(df.tail(10)["pnl"].sum()) / CAPITAL * 100, 2),
    }


def run_backtest(start: str = "2026-03-02", end: str = "2026-07-06") -> dict[str, Any]:
    os.environ["ETF_AGENT_STRICT_DATA"] = "1"
    os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"
    os.environ["ETF_AGENT_SKIP_NEWS_LLM"] = "1"

    dates = _trade_dates(start, end)
    results: dict[str, list[dict[str, Any]]] = {}

    for label, stable in (("original_no_stable", False), ("stable_overlay", True)):
        os.environ["ETF_AGENT_STABLE_MODE"] = "1" if stable else "0"
        reset_rotation_tracker()
        rows: list[dict[str, Any]] = []
        for trade_date in dates:
            recent_risk = _risk_context(rows, trade_date) if stable else None
            with contextlib.redirect_stdout(io.StringIO()):
                result = run_decision(trade_date, CAPITAL, recent_risk=recent_risk)
            comp = to_competition_output(result)
            pnl, used = _settle(comp, trade_date)
            rows.append({
                "date": trade_date,
                "pnl": pnl,
                "ret": pnl / CAPITAL,
                "used": used,
                "n": len(comp),
                "symbols": ",".join(item["symbol"] for item in comp),
                "invest_ratio": result.get("summary", {}).get("invest_ratio", 0.0),
                "stability_overlay": result.get("stability_overlay"),
            })
        results[label] = rows

    return {
        "start": start,
        "end": end,
        "stats": [
            _stats(results["original_no_stable"], "original_no_stable"),
            _stats(results["stable_overlay"], "stable_overlay"),
        ],
        "last15": {
            key: rows[-15:]
            for key, rows in results.items()
        },
    }


def main() -> int:
    payload = run_backtest()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

