"""Review the previous daily prediction using open-to-close ETF returns."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "daily_output"


def _load_bar(code: str, trade_date: str) -> dict[str, float] | None:
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path).rename(columns={
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
    })
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    row = df[df["date"] == pd.to_datetime(trade_date)]
    if row.empty:
        return None
    item = row.iloc[0]
    return {
        "open": float(item["open"]),
        "close": float(item["close"]),
    }


def _output_files_before(as_of: str) -> list[Path]:
    if not OUTPUT_DIR.exists():
        return []
    cutoff = pd.to_datetime(as_of).date()
    files = []
    for path in OUTPUT_DIR.glob("*_full.json"):
        try:
            d = pd.to_datetime(path.name.split("_")[0]).date()
        except Exception:
            continue
        if d < cutoff:
            files.append(path)
    return sorted(files)


def review_previous_prediction(as_of: str | None = None) -> dict[str, Any] | None:
    """Find the latest prediction before ``as_of`` and compute same-day P&L."""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    files = _output_files_before(as_of)
    if not files:
        return None

    path = files[-1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    trade_date = payload.get("date") or path.name.split("_")[0]
    picks = payload.get("competition_output") or []
    rows = []
    total = 0.0

    for pick in picks:
        code = str(pick.get("symbol") or "").zfill(6)
        volume = int(float(pick.get("volume") or 0))
        bar = _load_bar(code, trade_date)
        if not code or volume <= 0 or bar is None:
            continue
        pnl = (bar["close"] - bar["open"]) * volume
        rows.append({
            "symbol": code,
            "symbol_name": pick.get("symbol_name", ""),
            "volume": volume,
            "open": round(bar["open"], 4),
            "close": round(bar["close"], 4),
            "pnl": round(float(pnl), 2),
            "return_pct": round((bar["close"] / bar["open"] - 1) * 100, 3) if bar["open"] else 0.0,
        })
        total += pnl

    return {
        "review_date": date.today().strftime("%Y-%m-%d"),
        "prediction_date": trade_date,
        "source_file": str(path),
        "total_pnl": round(float(total), 2),
        "positions": rows,
    }


def write_pnl_report(report: dict[str, Any] | None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "last_pnl_report.txt"
    if report is None:
        path.write_text("暂无可复盘的上一日预测。\n", encoding="utf-8")
        return path

    lines = [
        f"预测日期: {report['prediction_date']}",
        f"单日收益: {report['total_pnl']:+.2f} 元",
        "",
    ]
    for row in report["positions"]:
        lines.append(
            f"{row['symbol']} {row['symbol_name']} vol={row['volume']} "
            f"open={row['open']} close={row['close']} "
            f"ret={row['return_pct']:+.3f}% pnl={row['pnl']:+.2f}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
