"""News time windows for open-before-09:30 decisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from market_data import latest_trade_date


def previous_trade_date(trade_date: str | date) -> date:
    """上一交易日：交易所日历（周末+法定休市），CSV 缺口作补充。"""
    from trading_calendar import previous_trading_day

    return previous_trading_day(trade_date)


def post_close_cutoff(trade_date: str | date) -> datetime:
    """上一交易日 15:00 — 盘后至今日开盘前的「未连续竞价定价」起点。"""
    prev = previous_trade_date(trade_date)
    return datetime(prev.year, prev.month, prev.day, 15, 0, 0)


def monday_hot_cutoff(trade_date: str | date) -> datetime | None:
    """长间隔开盘（周末/节假日后）的主 fresh 起点：开盘前一日 18:00。

    上一交易日与今日间隔 >1 个自然日时启用（周一、节后首日等）：
    桥接段（上一日 15:00 → 开盘前夜 18:00）降权进 stale，避免整段休市新闻
    都以 25% fresh 权重灌进评分。连续交易日返回 None。
    """
    d = pd.to_datetime(trade_date).date()
    prev = previous_trade_date(d)
    if (d - prev).days <= 1:
        return None
    eve = d - timedelta(days=1)
    return datetime(eve.year, eve.month, eve.day, 18, 0, 0)


def _parse_pub(art: dict[str, Any]) -> datetime | None:
    pub_str = str(art.get("published_at") or "").strip()
    if not pub_str:
        return None
    try:
        return datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def split_articles_by_post_close(
    articles: list[dict],
    trade_date: str | date,
) -> tuple[list[dict], list[dict], datetime]:
    """> 上一交易日15:00 为 fresh，其余为 stale。

    长间隔开盘（周末/节后）：仅开盘前夜 18:00 之后进 fresh；
    上一交易日 15:00～开盘前夜 18:00 并入 stale（降权）；
    无时间戳在长间隔日也进 stale，避免灌入主 fresh。
    连续交易日：无时间戳仍归 fresh（与旧行为一致）。
    """
    cutoff = post_close_cutoff(trade_date)
    hot_cut = monday_hot_cutoff(trade_date)
    fresh: list[dict] = []
    stale: list[dict] = []

    for art in articles:
        pub_ts = _parse_pub(art)
        if pub_ts is None:
            # 长间隔缺时间戳不进主 fresh，防止休市抓取噪声抬高仓位档位
            (stale if hot_cut is not None else fresh).append(art)
            continue
        if pub_ts <= cutoff:
            stale.append(art)
            continue
        # 已过上一日 15:00
        if hot_cut is not None and pub_ts < hot_cut:
            stale.append(art)  # 休市桥接段 → 降权（走 stale 10% 权重）
            continue
        fresh.append(art)

    return fresh, stale, cutoff


def describe_split_policy(trade_date: str | date) -> str:
    """日志用：说明当日 fresh/stale 切分策略。"""
    d = pd.to_datetime(trade_date).date()
    cutoff = post_close_cutoff(d)
    hot = monday_hot_cutoff(d)
    if hot is None:
        return f"标准切分: >{cutoff.strftime('%m-%d %H:%M')} 为 fresh"
    return (
        f"长间隔切分: 主fresh≥{hot.strftime('%m-%d %H:%M')}(开盘前夜~开盘前); "
        f"休市桥接 {cutoff.strftime('%m-%d %H:%M')}~{hot.strftime('%m-%d %H:%M')} 降权并入stale"
    )
