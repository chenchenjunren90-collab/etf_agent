"""Backtest current strategy (stable + concentration) on local CSVs.

Uses platform settlement: prev close buy -> same-day close sell.
Passes integrity_ctx so concentration risk is included.
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
from decision_integrity import (
    apply_concentration_risk,
    compute_sole_symbol_streak,
)
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


def _integrity_from_history(rows: list[dict[str, Any]], trade_date: str) -> dict[str, Any]:
    """Build integrity_ctx from simulated prior submits (no price-stale in offline BT)."""
    history = []
    for row in rows:
        syms = [s for s in str(row.get("symbols") or "").split(",") if s]
        history.append({"date": row["date"], "symbols": syms})
    streak = compute_sole_symbol_streak(history)
    return {
        "price_audit": {
            "decision_date": trade_date,
            "price_stale": False,
            "stale_ratio": 0.0,
            "expected_bar_date": None,
        },
        "price_stale": False,
        "block_llm_rescore": False,
        "recent_submit_history": history[-6:],
        "sole_symbol_streak": streak,
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
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    curve = (1 + returns).cumprod()
    max_drawdown = float(((curve / curve.cummax()) - 1).min()) if len(curve) else 0.0
    return {
        "label": label,
        "days": int(len(df)),
        "total_pnl": round(total_pnl, 2),
        "total_ret_pct": round(total_pnl / CAPITAL * 100, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 1) if len(returns) else 0.0,
        "avg_used_pct": round(float((df["used"] / CAPITAL).mean()) * 100, 1) if len(df) else 0.0,
        "avg_positions": round(float(df["n"].mean()), 2) if len(df) else 0.0,
        "sole_name_days": int((df["n"] == 1).sum()) if len(df) else 0,
        "sharpe_ann": round(float(returns.mean()) / std * math.sqrt(252), 2) if std else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "last10_pnl": round(float(df.tail(10)["pnl"].sum()), 2) if len(df) else 0.0,
        "last10_ret_pct": round(float(df.tail(10)["pnl"].sum()) / CAPITAL * 100, 2) if len(df) else 0.0,
    }


def run_variant(
    dates: list[str],
    *,
    stable: bool,
    concentration: bool,
) -> list[dict[str, Any]]:
    os.environ["ETF_AGENT_STABLE_MODE"] = "1" if stable else "0"
    reset_rotation_tracker()
    rows: list[dict[str, Any]] = []
    for trade_date in dates:
        recent_risk = _risk_context(rows, trade_date) if stable else None
        integrity_ctx = _integrity_from_history(rows, trade_date) if concentration else None
        with contextlib.redirect_stdout(io.StringIO()):
            result = run_decision(
                trade_date,
                CAPITAL,
                recent_risk=recent_risk,
                integrity_ctx=integrity_ctx,
            )
        # If concentration disabled, strip any accidental application by not passing ctx.
        # If enabled but run_decision already applied it via integrity_ctx — good.
        # Extra safety: when concentration=False we already pass None.
        if concentration and integrity_ctx and result.get("ranked"):
            # already applied inside run_decision; nothing else needed
            pass
        comp = to_competition_output(result)
        pnl, used = _settle(comp, trade_date)
        conc = result.get("concentration_risk") or {}
        rows.append({
            "date": trade_date,
            "pnl": round(pnl, 2),
            "ret": pnl / CAPITAL,
            "used": used,
            "n": len(comp),
            "symbols": ",".join(item["symbol"] for item in comp),
            "invest_ratio": (result.get("summary") or {}).get("invest_ratio", 0.0),
            "concentration_applied": bool(conc.get("applied")),
        })
    return rows


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-03-02")
    parser.add_argument("--end", default="2026-07-09")
    args = parser.parse_args()

    os.environ["ETF_AGENT_STRICT_DATA"] = "1"
    os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"
    os.environ["ETF_AGENT_SKIP_NEWS_LLM"] = "1"

    dates = _trade_dates(args.start, args.end)
    print(f"Backtest {args.start} → {args.end} ({len(dates)} trade days)\n")

    variants = [
        ("A_no_stable_no_conc", False, False),
        ("B_stable_only", True, False),
        ("C_stable_plus_concentration", True, True),
    ]
    all_rows: dict[str, list[dict[str, Any]]] = {}
    stats = []
    for label, stable, conc in variants:
        print(f"Running {label} ...")
        rows = run_variant(dates, stable=stable, concentration=conc)
        all_rows[label] = rows
        s = _stats(rows, label)
        stats.append(s)
        print(
            f"  pnl={s['total_pnl']:+.0f} ({s['total_ret_pct']:+.2f}%) "
            f"win={s['win_rate_pct']}% sharpe={s['sharpe_ann']} "
            f"mdd={s['max_drawdown_pct']}% last10={s['last10_pnl']:+.0f} "
            f"sole_days={s['sole_name_days']}"
        )

    print("\n=== SUMMARY ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    print("\n=== LAST 15 DAYS (C_stable_plus_concentration) ===")
    for row in all_rows["C_stable_plus_concentration"][-15:]:
        flag = " [CONC]" if row.get("concentration_applied") else ""
        print(
            f"{row['date']} n={row['n']} pnl={row['pnl']:+8.1f} "
            f"used={row['used']/CAPITAL*100:4.1f}% {row['symbols']}{flag}"
        )

    out = BASE_DIR / "data" / "backtest_current_strategy.json"
    out.write_text(
        json.dumps(
            {"start": args.start, "end": args.end, "stats": stats, "last15": {
                k: v[-15:] for k, v in all_rows.items()
            }},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
