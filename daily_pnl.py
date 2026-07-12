"""Review the previous daily prediction using platform settlement returns.

平台结算口径（investment-daily-submit.html）：买入价=前一交易日收盘价，
卖出价=当日收盘价，pnl = amount × (今收-昨收) / 昨收。见 settlement_prices.py。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from settlement_prices import get_close_to_close

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "daily_output"


def _load_bar(code: str, trade_date: str) -> dict[str, float] | None:
    """返回 {"prev_close": 前一交易日收盘价, "close": 当日收盘价}（平台结算口径）。"""
    prices = get_close_to_close(code, trade_date, data_dir=DATA_DIR)
    if prices is None:
        return None
    prev_close, today_close = prices
    return {"prev_close": prev_close, "close": today_close}


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
    """Find the latest prediction before ``as_of`` and settle it 昨收→今收（平台口径）。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    files = _output_files_before(as_of)
    if not files:
        return None

    path = None
    payload: dict[str, Any] = {}
    for candidate in reversed(files):
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if loaded.get("mode") in {"personal_sandbox", "fatal_fallback"}:
            continue
        path = candidate
        payload = loaded
        break
    if path is None:
        return None
    trade_date = payload.get("date") or path.name.split("_")[0]
    picks = payload.get("competition_output") or []
    rows = []
    total = 0.0
    unsettled: list[str] = []

    for pick in picks:
        code = str(pick.get("symbol") or "").zfill(6)
        volume = int(float(pick.get("volume") or 0))
        bar = _load_bar(code, trade_date)
        if not code or volume <= 0:
            continue
        if bar is None:
            unsettled.append(code)
            continue
        prev_close = bar["prev_close"]
        close = bar["close"]
        pnl = (close - prev_close) * volume
        rows.append({
            "symbol": code,
            "symbol_name": pick.get("symbol_name", ""),
            "volume": volume,
            "prev_close": round(prev_close, 4),
            "close": round(close, 4),
            "pnl": round(float(pnl), 2),
            "return_pct": round((close / prev_close - 1) * 100, 3) if prev_close else 0.0,
        })
        total += pnl

    return {
        "review_date": date.today().strftime("%Y-%m-%d"),
        "prediction_date": trade_date,
        "source_file": str(path),
        "total_pnl": round(float(total), 2),
        "positions": rows,
        "pending": bool(unsettled),
        "unsettled_symbols": unsettled,
    }


def write_pnl_report(report: dict[str, Any] | None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / "last_pnl_report.txt"
    if report is None:
        path.write_text("暂无可复盘的上一日预测。\n", encoding="utf-8")
        return path

    lines = [
        f"预测日期: {report['prediction_date']}",
        (
            "单日收益: 待完整行情后结算"
            if report.get("pending")
            else f"单日收益: {report['total_pnl']:+.2f} 元（按平台口径：昨收→今收）"
        ),
        "",
    ]
    if report.get("pending"):
        lines.append("未结算标的: " + ", ".join(report.get("unsettled_symbols") or []))
    for row in report["positions"]:
        lines.append(
            f"{row['symbol']} {row['symbol_name']} vol={row['volume']} "
            f"昨收={row['prev_close']} 今收={row['close']} "
            f"ret={row['return_pct']:+.3f}% pnl={row['pnl']:+.2f}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
