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

from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"


def get_close_to_close(code: str, trade_date: str, *, data_dir: Path | None = None) -> tuple[float, float] | None:
    """返回 (前一交易日收盘价, 当日收盘价)；缺数据或无昨收时返回 None。"""
    path = (data_dir or DATA_DIR) / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path).rename(columns={
            "日期": "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close",
        })
    except Exception:
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)

    target = pd.to_datetime(trade_date, errors="coerce")
    if pd.isna(target):
        return None
    matches = df.index[df["date"] == target]
    if len(matches) == 0:
        return None
    i = int(matches[0])
    if i == 0:
        return None  # 没有更早一个交易日的收盘价，无法计算昨收

    prev_close = float(df.loc[i - 1, "close"])
    today_close = float(df.loc[i, "close"])
    if prev_close <= 0:
        return None
    return prev_close, today_close


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
