"""Fetch CCTV 新闻联播 + 百度财经经济日历 into the historical news SQLite store.

Both sources are reached through ``akshare`` and intentionally use deterministic
synthetic URLs so the existing SQLite UNIQUE-on-url constraint deduplicates
them across runs.

Usage:
    py -3 news_aux_fetcher.py --source cctv --start 2026-03-01 --end 2026-04-30
    py -3 news_aux_fetcher.py --source economic --start 2026-03-01 --end 2026-04-30
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from market_data import _no_proxy_env
from news_store import connect, stats, upsert_article


def _date_range(start: str, end: str) -> list[str]:
    start_dt = datetime.strptime(start, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end, "%Y-%m-%d").date()
    out = []
    cur = start_dt
    while cur <= end_dt:
        out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def _import_ak():
    try:
        import akshare as ak
    except Exception as exc:
        raise RuntimeError(f"akshare not available: {exc}") from exc
    return ak


# 新闻联播首播 19:00。该时刻 < T+1 09:30 开盘，预测下一交易日可用。
CCTV_PUBLISH_HM = "19:00:00"
# 经济日历用盘前 07:00 作为发布时间（绝大多数公布前已知预告/前值）。
ECON_PUBLISH_HM = "07:00:00"


def fetch_cctv_day(date: str) -> list[dict[str, Any]]:
    """Fetch one day of CCTV 新闻联播."""
    ak = _import_ak()
    with _no_proxy_env():
        try:
            df = ak.news_cctv(date=date)
        except Exception as exc:
            print(f"  ! cctv {date} failed: {exc}", flush=True)
            return []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    articles = []
    iso_date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    for idx, row in df.iterrows():
        title = str(row.get("title") or "").strip()
        content = str(row.get("content") or "").strip()
        if not title:
            continue
        articles.append({
            "url": f"cctv://xwlb/{date}#{idx:04d}",
            "title": title,
            "publish_time": f"{iso_date} {CCTV_PUBLISH_HM}",
            "source": "央视新闻联播",
            "channel": "cctv_xwlb",
            "content": content,
        })
    return articles


def _format_economic_event(row: pd.Series) -> str:
    """Render one economic-calendar row into a human-readable phrase."""
    bits = []
    region = str(row.get("地区") or "").strip()
    if region:
        bits.append(region)
    event = str(row.get("事件") or "").strip()
    if event:
        bits.append(event)
    pub = row.get("公布")
    exp = row.get("预期")
    prev = row.get("前值")
    nums = []
    if pd.notna(pub):
        nums.append(f"公布{pub}")
    if pd.notna(exp):
        nums.append(f"预期{exp}")
    if pd.notna(prev):
        nums.append(f"前值{prev}")
    if nums:
        bits.append(" / ".join(nums))
    return " ".join(bits)


def fetch_economic_day(date: str, *, min_importance: int = 2) -> list[dict[str, Any]]:
    """Fetch one day of 百度财经-经济数据日历 and aggregate into a summary article."""
    ak = _import_ak()
    with _no_proxy_env():
        try:
            df = ak.news_economic_baidu(date=date)
        except Exception as exc:
            print(f"  ! economic {date} failed: {exc}", flush=True)
            return []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []

    df = df.copy()
    df["重要性"] = pd.to_numeric(df["重要性"], errors="coerce").fillna(0).astype(int)
    important = df[df["重要性"] >= min_importance].copy()
    if important.empty:
        return []

    # 优先放重要性高的、然后按时间排
    important.sort_values(["重要性", "时间"], ascending=[False, True], inplace=True)
    iso_date = f"{date[:4]}-{date[4:6]}-{date[6:]}"

    title_bits = []
    for _, row in important.head(5).iterrows():
        ev = str(row.get("事件") or "").strip()
        region = str(row.get("地区") or "").strip()
        if ev:
            title_bits.append(f"{region}{ev}")
    title = f"{iso_date} 经济数据公布: " + "; ".join(title_bits)[:200]

    content_bits = []
    for _, row in important.iterrows():
        line = _format_economic_event(row)
        if line:
            content_bits.append(line)
    content = " | ".join(content_bits)

    return [{
        "url": f"baidu_econ://{date}#summary",
        "title": title,
        "publish_time": f"{iso_date} {ECON_PUBLISH_HM}",
        "source": "百度财经-经济数据日历",
        "channel": "baidu_economic",
        "content": content,
    }]


def crawl_source(source: str, *, start: str, end: str, sleep_seconds: float = 0.5) -> dict[str, int]:
    counters = {"days": 0, "fetched": 0, "saved": 0, "duplicates": 0, "failed": 0}
    days = _date_range(start, end)
    with connect() as conn:
        for date in days:
            counters["days"] += 1
            try:
                if source == "cctv":
                    articles = fetch_cctv_day(date)
                elif source == "economic":
                    articles = fetch_economic_day(date)
                else:
                    raise ValueError(f"unknown source: {source}")
            except Exception as exc:
                counters["failed"] += 1
                print(f"  ! {source} {date} failed: {exc}", flush=True)
                continue

            counters["fetched"] += len(articles)
            saved_today = 0
            for article in articles:
                if upsert_article(conn, article):
                    saved_today += 1
                    counters["saved"] += 1
                else:
                    counters["duplicates"] += 1
            conn.commit()
            print(f"[{source}] {date} fetched={len(articles)} saved={saved_today}", flush=True)
            time.sleep(sleep_seconds)
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch CCTV/economic news into SQLite for backtesting.")
    parser.add_argument("--source", choices=["cctv", "economic"], required=True)
    parser.add_argument("--start", required=True, help="e.g. 2026-03-01")
    parser.add_argument("--end", required=True, help="e.g. 2026-04-30")
    parser.add_argument("--sleep", type=float, default=0.5)
    args = parser.parse_args()

    counters = crawl_source(args.source, start=args.start, end=args.end, sleep_seconds=args.sleep)
    print("\nFetch summary:", counters)
    print("DB stats:", stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
