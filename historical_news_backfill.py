"""Build an explicitly retrospective Eastmoney news panel for research.

This helper is not a substitute for an immutable decision-time snapshot.  It
uses a fixed, checked-in query vocabulary and records retrieval provenance so
backtests can distinguish reconstructed history from news actually archived
on the decision date.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from news_time_split import decision_cutoff, post_close_cutoff


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "historical_news_backfill"

# Fixed before looking at any return labels.  These terms mirror the permanent
# ETF pool and broad event mechanisms; they must not be tuned per backtest day.
FIXED_QUERY_TERMS = (
    "510300", "510050", "510500", "159338", "510880", "512880", "512010",
    "159915", "588000", "159949",
    "沪深300", "上证50", "中证500", "中证A500", "红利指数", "券商行业",
    "医药行业", "创业板", "科创板", "半导体行业", "人工智能行业",
    "A股政策", "央行政策", "证监会政策", "宏观数据",
)


def _fetch_page(keyword: str, page_index: int, page_size: int = 100) -> pd.DataFrame:
    """Call AkShare while overriding the Eastmoney page parameters."""
    import akshare as ak
    import akshare.news.news_stock as news_module

    original_get = news_module.requests.get

    def paged_get(url: str, *args: Any, **kwargs: Any):
        params = dict(kwargs.get("params") or {})
        inner = json.loads(params["param"])
        config = inner["param"]["cmsArticleWebOld"]
        config["pageIndex"] = int(page_index)
        config["pageSize"] = int(page_size)
        params["param"] = json.dumps(inner, ensure_ascii=False)
        kwargs["params"] = params
        return original_get(url, *args, **kwargs)

    news_module.requests.get = paged_get
    try:
        frame = ak.stock_news_em(keyword)
    finally:
        news_module.requests.get = original_get
    return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame()


def fetch_retrospective_panel(
    start: str,
    end: str,
    *,
    max_pages: int = 8,
    sleep_seconds: float = 0.15,
) -> dict[str, Any]:
    start_ts = pd.to_datetime(start) - pd.Timedelta(days=4)
    end_ts = pd.to_datetime(end) + pd.Timedelta(hours=9, minutes=30)
    records: list[dict[str, Any]] = []
    query_audit: dict[str, Any] = {}

    for query_index, keyword in enumerate(FIXED_QUERY_TERMS, 1):
        query_rows = 0
        failures: list[str] = []
        oldest_seen: pd.Timestamp | None = None
        for page in range(1, max_pages + 1):
            try:
                frame = _fetch_page(keyword, page)
            except Exception as exc:
                failures.append(f"page_{page}:{str(exc)[:100]}")
                break
            if frame.empty:
                break

            page_times = pd.to_datetime(frame.iloc[:, 3], errors="coerce")
            valid_times = page_times.dropna()
            if len(valid_times):
                page_oldest = valid_times.min()
                oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)

            for row_index, (_, row) in enumerate(frame.iterrows()):
                published = pd.to_datetime(row.iloc[3], errors="coerce")
                if pd.isna(published) or not (start_ts <= published <= end_ts):
                    continue
                title = str(row.iloc[1] or "").strip()
                if not title:
                    continue
                records.append({
                    "title": title,
                    "content": str(row.iloc[2] or "").strip(),
                    "source": str(row.iloc[4] or "eastmoney_search").strip(),
                    "published_at": published.strftime("%Y-%m-%d %H:%M:%S"),
                    "url": str(row.iloc[5] or "").strip(),
                    "retrospective_query": keyword,
                    "retrospective": True,
                    "search_page": page,
                    "search_row": row_index,
                })
                query_rows += 1

            if oldest_seen is not None and oldest_seen < start_ts and page >= 3:
                break
            time.sleep(max(0.0, sleep_seconds))

        query_audit[keyword] = {
            "matched_window_rows": query_rows,
            "oldest_seen": oldest_seen.isoformat() if oldest_seen is not None else None,
            "failures": failures,
        }
        print(
            f"[{query_index:02d}/{len(FIXED_QUERY_TERMS)}] {keyword}: "
            f"window_rows={query_rows} failures={len(failures)}",
            flush=True,
        )

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in sorted(records, key=lambda item: item["published_at"], reverse=True):
        key = str(article.get("url") or "").strip() or (
            f"{article.get('published_at')}|{article.get('source')}|{article.get('title')}"
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(article)

    return {
        "kind": "retrospective_search_panel",
        "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "start": start,
        "end": end,
        "query_terms": list(FIXED_QUERY_TERMS),
        "query_audit": query_audit,
        "article_count": len(deduped),
        "articles": deduped,
        "limitations": [
            "Retrieved after the fact; publication timestamps are not immutable capture proof.",
            "Search-index ranking and deletions may create survivorship or selection bias.",
            "Use for sensitivity analysis only, never as the official historical track record.",
        ],
    }


def coverage_by_trade_date(payload: dict[str, Any]) -> dict[str, Any]:
    articles = list(payload.get("articles") or [])
    start = pd.to_datetime(payload["start"])
    end = pd.to_datetime(payload["end"])
    dates = pd.bdate_range(start, end)
    coverage: dict[str, Any] = {}
    for value in dates:
        trade_date = value.strftime("%Y-%m-%d")
        lower = post_close_cutoff(trade_date)
        upper = decision_cutoff(trade_date, "09:30")
        rows = [
            article for article in articles
            if lower < datetime.strptime(article["published_at"], "%Y-%m-%d %H:%M:%S") <= upper
        ]
        coverage[trade_date] = {
            "fresh_count": len(rows),
            "sources": len({str(article.get("source") or "") for article in rows}),
            "queries": len({str(article.get("retrospective_query") or "") for article in rows}),
        }
    return coverage


def main() -> int:
    parser = argparse.ArgumentParser(description="Build retrospective historical news panel")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--sleep", type=float, default=0.15)
    args = parser.parse_args()

    payload = fetch_retrospective_panel(
        args.start,
        args.end,
        max_pages=max(1, args.max_pages),
        sleep_seconds=max(0.0, args.sleep),
    )
    payload["coverage"] = coverage_by_trade_date(payload)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{args.start}_{args.end}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {path} ({payload['article_count']} unique articles)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
