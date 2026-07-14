"""比赛结算价格工具：昨收 -> 今收。

投资建议提交页（investment-daily-submit.html）的结算口径明确写明：
    买入参考价 = 前一交易日收盘价（amount = volume × 昨收）
    卖出价     = 当日收盘价（收盘后自动卖出，不留隔夜持仓）
    单笔盈亏   = amount × (当日收盘 − 昨收) / 昨收

选股/择时逻辑本身不受影响：features.py 的 ret_1d/ret_3d/ret_5d 等趋势特征
本来就是收盘价对收盘价计算，与平台结算口径天然一致。

半截K说明：close≈open 且振幅大 在 Baostock 上也常见（真实平收日），
不能单独作为拒结算条件。脏价应在拉取阶段用 AkShare≠Baostock 回退修复
（见 market_data.fetch_etf_hist / repair_incomplete_history）。
"""
from __future__ import annotations

import os
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_calendar import is_trading_day, previous_trading_day

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DEFAULT_MIN_COMPLETION_VOLUME_RATIO = 0.05


def _ready_time() -> time:
    raw = os.environ.get("ETF_SETTLEMENT_READY_TIME", "16:15").strip()
    try:
        hour, minute = (int(part) for part in raw.split(":", 1))
        return time(hour, minute)
    except (TypeError, ValueError):
        return time(16, 15)


def _min_completion_volume_ratio() -> float:
    raw = os.environ.get(
        "ETF_SETTLEMENT_MIN_VOLUME_RATIO",
        str(DEFAULT_MIN_COMPLETION_VOLUME_RATIO),
    )
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_COMPLETION_VOLUME_RATIO


def shanghai_now(as_of: datetime | None = None) -> datetime:
    if as_of is None:
        return datetime.now(ZoneInfo("Asia/Shanghai"))
    if as_of.tzinfo is None:
        return as_of.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return as_of.astimezone(ZoneInfo("Asia/Shanghai"))


def settlement_ready(trade_date: str, *, as_of: datetime | None = None) -> bool:
    """Return whether a daily bar may be treated as final in Shanghai time."""
    target = pd.to_datetime(trade_date, errors="coerce")
    if pd.isna(target):
        return False
    now = shanghai_now(as_of)
    target_date = target.date()
    if target_date < now.date():
        return True
    if target_date > now.date() or not is_trading_day(target_date):
        return False
    return now.time() >= _ready_time()


def get_close_to_close(
    code: str,
    trade_date: str,
    *,
    data_dir: Path | None = None,
    as_of: datetime | None = None,
    require_complete: bool = True,
) -> tuple[float, float] | None:
    """返回 (前一交易日收盘价, 当日收盘价)；缺数据或无昨收时返回 None。"""
    path = (data_dir or DATA_DIR) / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path).rename(columns={
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
        })
    except Exception:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    target = pd.to_datetime(trade_date, errors="coerce")
    if pd.isna(target):
        return None
    target = target.normalize()
    if not is_trading_day(target):
        return None
    now = shanghai_now(as_of)
    if target.date() > now.date():
        return None
    matches = df.index[df["date"] == target]
    if len(matches) == 0:
        return None
    i = int(matches[0])
    expected_prev = pd.Timestamp(previous_trading_day(target))
    prev_matches = df.index[df["date"] == expected_prev]
    if len(prev_matches) == 0:
        return None
    prev_i = int(prev_matches[-1])
    if prev_i >= i:
        return None

    if require_complete and target.date() == now.date():
        if now.time() < _ready_time():
            return None
        row = df.loc[i]
        if "volume" in df.columns:
            try:
                current_volume = float(row.get("volume") or 0.0)
                if current_volume <= 0:
                    return None
            except (TypeError, ValueError):
                return None
            historical_volume = pd.to_numeric(
                df.loc[df.index < i, "volume"], errors="coerce"
            ).dropna().tail(20)
            historical_volume = historical_volume[historical_volume > 0]
            if len(historical_volume) >= 2:
                normal_volume = float(historical_volume.median())
                min_ratio = _min_completion_volume_ratio()
                if normal_volume > 0 and current_volume < normal_volume * min_ratio:
                    return None
        try:
            from market_data import bar_row_looks_incomplete

            if bar_row_looks_incomplete(row):
                return None
        except Exception:
            pass

    prev_close = float(df.loc[prev_i, "close"])
    today_close = float(df.loc[i, "close"])
    if prev_close <= 0:
        return None
    return prev_close, today_close


def settle_competition_output(
    items: list[dict[str, Any]],
    trade_date: str,
    *,
    data_dir: Path | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Settle every executable position while retaining incomplete-day evidence."""
    pnl = 0.0
    settled_count = 0
    unsettled_symbols: list[str] = []
    position_count = 0
    for item in items:
        position_count += 1
        raw_code = str(item.get("symbol") or "").strip()
        code = raw_code.zfill(6) if raw_code else ""
        try:
            volume = int(float(item.get("volume") or 0))
        except (TypeError, ValueError):
            volume = 0
        prices = get_close_to_close(
            code,
            trade_date,
            data_dir=data_dir,
            as_of=as_of,
        )
        if not code or volume <= 0 or prices is None:
            unsettled_symbols.append(code or "invalid")
            continue
        prev_close, today_close = prices
        pnl += volume * (today_close - prev_close)
        settled_count += 1
    return {
        "pnl": float(pnl),
        "position_count": position_count,
        "settled_count": settled_count,
        "unsettled_symbols": unsettled_symbols,
        "complete": settled_count == position_count,
    }


def conservative_risk_pnl(settlement: dict[str, Any]) -> float | None:
    """Use complete PnL, or only a negative partial PnL, never partial gains."""
    pnl = float(settlement.get("pnl") or 0.0)
    if settlement.get("complete"):
        return pnl
    if int(settlement.get("settled_count") or 0) > 0 and pnl < 0:
        return pnl
    return None


def settle_pnl(code: str, trade_date: str, volume: int, *, data_dir: Path | None = None) -> dict | None:
    """按平台口径结算单只 ETF 当日盈亏：amount=volume×昨收，pnl=amount×(今收-昨收)/昨收。"""
    prices = get_close_to_close(code, trade_date, data_dir=data_dir)
    if prices is None or volume <= 0:
        return None
    prev_close, today_close = prices
    amount = volume * prev_close
    pnl = amount * (today_close - prev_close) / prev_close
    return {
        "prev_close": round(prev_close, 4),
        "close": round(today_close, 4),
        "amount": round(amount, 2),
        "pnl": round(pnl, 2),
        "return_pct": round((today_close / prev_close - 1) * 100, 3),
    }
