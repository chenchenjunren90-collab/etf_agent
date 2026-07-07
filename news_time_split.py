"""News time windows for open-before-09:30 decisions."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from market_data import latest_trade_date


def previous_trade_date(trade_date: str | date) -> date:
    """上一交易日（跳过周末）。"""
    d = pd.to_datetime(trade_date).date()
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def post_close_cutoff(trade_date: str | date) -> datetime:
    """上一交易日 15:00 — 盘后至今日开盘前的「新鲜新闻」起点。"""
    prev = previous_trade_date(trade_date)
    return datetime(prev.year, prev.month, prev.day, 15, 0, 0)


def split_articles_by_post_close(
    articles: list[dict],
    trade_date: str | date,
) -> tuple[list[dict], list[dict], datetime]:
    """> 上一交易日15:00 为 fresh，其余为 stale；无时间戳归 fresh。"""
    cutoff = post_close_cutoff(trade_date)
    fresh: list[dict] = []
    stale: list[dict] = []
    for art in articles:
        pub_str = art.get("published_at", "")
        if not pub_str:
            fresh.append(art)
            continue
        try:
            pub_ts = datetime.strptime(pub_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            fresh.append(art)
            continue
        if pub_ts > cutoff:
            fresh.append(art)
        else:
            stale.append(art)
    return fresh, stale, cutoff
