"""Daily news collection for the ETF agent.

Only this module touches external news APIs.  It intentionally returns a simple
article list so the screening policy in ``news_signal.py`` remains testable and
independent from AkShare quirks.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from market_data import _no_proxy_env
from strategy import TRADING_POOL


TITLE_COLUMNS = ("标题", "title", "新闻标题", "tag")
CONTENT_COLUMNS = ("摘要", "内容", "digest", "content", "新闻内容", "summary")
URL_COLUMNS = ("链接", "网址", "url", "URL")


def _first_value(row: pd.Series, candidates: tuple[str, ...]) -> str:
    for col in candidates:
        if col in row and pd.notna(row[col]):
            value = str(row[col]).strip()
            if value:
                return value
    return ""


def _parse_time(value: str, trade_date: str) -> datetime | None:
    if not value:
        return None
    now = datetime.now()
    normalized = value.strip()
    if "分钟前" in normalized or "刚刚" in normalized:
        return now
    if "小时前" in normalized:
        try:
            hours = int(normalized.split("小时前")[0].strip())
            return now - timedelta(hours=hours)
        except Exception:
            return now
    parsed = pd.to_datetime(normalized, errors="coerce")
    if pd.notna(parsed):
        ts = parsed.to_pydatetime()
        if ts.year == 1900:
            base = pd.to_datetime(trade_date).to_pydatetime()
            return base.replace(hour=ts.hour, minute=ts.minute, second=ts.second)
        return ts
    return None


def _fetch_window_start(trade_date: str, cutoff_time: str) -> datetime:
    """抓取窗口起点：上一交易日 15:00 再往前 48h（供 stale 层），周末/节假日随日历拉长。

    旧实现固定 cutoff-60h，周一 09:30 起点落在周六上午，会丢掉周五盘后新闻。
    """
    cutoff = pd.to_datetime(f"{trade_date} {cutoff_time}").to_pydatetime()
    try:
        from news_time_split import post_close_cutoff

        return post_close_cutoff(trade_date) - timedelta(hours=48)
    except Exception:
        return cutoff - timedelta(hours=84)


def _before_cutoff(ts: datetime | None, trade_date: str, cutoff_time: str) -> bool:
    if ts is None:
        # 无法证明在决策截止前已公开的新闻不得进入实时评分。
        return False
    cutoff = pd.to_datetime(f"{trade_date} {cutoff_time}").to_pydatetime()
    start = _fetch_window_start(trade_date, cutoff_time)
    return start <= ts <= cutoff


def _articles_from_df(
    df: pd.DataFrame | None,
    *,
    source: str,
    trade_date: str,
    cutoff_time: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if df is None or len(df) == 0:
        return []
    out: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        title = _first_value(row, TITLE_COLUMNS)
        if not title:
            continue
        date_part = _first_value(row, ("发布日期", "日期", "date"))
        time_part = _first_value(row, ("发布时间", "时间", "publish_time", "datetime"))
        combined = " ".join(p for p in (date_part, time_part) if p).strip()
        published_at = _parse_time(combined, trade_date) if combined else None
        if not _before_cutoff(published_at, trade_date, cutoff_time):
            continue
        content = _first_value(row, CONTENT_COLUMNS)
        out.append({
            "title": title,
            "content": content,
            "source": source,
            "published_at": published_at.strftime("%Y-%m-%d %H:%M:%S") if published_at else "",
            "url": _first_value(row, URL_COLUMNS),
        })
    return out


def _try_ak_call(name: str, *args: Any, timeout: float = 30.0, **kwargs: Any) -> pd.DataFrame | None:
    """调用 AkShare 接口并设置超时保护，避免单个源卡死整个流程。"""
    try:
        import akshare as ak
    except Exception:
        return None

    func = getattr(ak, name, None)
    if func is None:
        return None
    with _no_proxy_env():
        # 修复 curl CAfile 路径无效导致 akshare 网络请求失败
        try:
            import certifi as _certifi
            import os as _os
            _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
            _os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
        except ImportError:
            pass
        try:
            from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(func, *args, **kwargs)
                try:
                    df = future.result(timeout=timeout)
                except FuturesTimeout:
                    print(f"  [WARN] AkShare {name} 超时 ({timeout:.0f}s), 跳过")
                    return None
            return df if isinstance(df, pd.DataFrame) else None
        except TypeError:
            return None
        except Exception:
            return None


def fetch_news_articles(
    trade_date: str,
    *,
    cutoff_time: str = "09:30",
) -> list[dict[str, Any]]:
    """Fetch latest finance/news articles available before ``cutoff_time``."""
    articles: list[dict[str, Any]] = []

    # Broad market feeds.  Names differ across AkShare versions, so every call is
    # guarded and silently skipped if unavailable.
    broad_sources = (
        ("stock_info_global_em", (), {}, "eastmoney_global"),
        ("stock_info_global_ths", (), {}, "ths_global"),
        # ("stock_info_global_cls", (), {}, "cls_global"),  # 财联社源超时(>500s)且返回0条，已移除
        ("stock_info_global_sina", (), {}, "sina_global"),
        ("stock_info_global_futu", (), {}, "futu_global"),
        ("stock_info_cjzc_em", (), {}, "eastmoney_cjzc"),
        ("stock_news_main_cx", (), {}, "caixin_main"),
    )
    for func_name, args, kwargs, source in broad_sources:
        df = _try_ak_call(func_name, *args, **kwargs)
        articles.extend(_articles_from_df(df, source=source, trade_date=trade_date, cutoff_time=cutoff_time))

    # ETF/stock-specific Eastmoney feeds mostly return automated "资金榜 / 融资榜"
    # data ticks, which are not the PDF-style substantive catalysts.  Skip them
    # entirely; macro feeds above provide the events we actually want.

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in articles:
        key = article["title"].strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(article)
    return deduped
