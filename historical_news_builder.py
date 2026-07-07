"""Build historical strict-news signals from the local news SQLite store."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from news_signal import build_news_signal
from news_llm_scorer import score_news_with_llm, merge_llm_into_news_signal
from news_store import query_articles_before
from strategy import TRADING_POOL, _calc_short_race_features


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUT_DIR = DATA_DIR / "historical_news_signal"


def _read_price(code: str, trade_date: str) -> pd.DataFrame | None:
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path).rename(columns={
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        })
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        cutoff = pd.to_datetime(trade_date)
        df = df[df["date"] < cutoff].copy()
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    except Exception:
        return None


def _consecutive_up_days(df: pd.DataFrame) -> int:
    close = df["close"].dropna()
    count = 0
    for i in range(len(close) - 1, 0, -1):
        if float(close.iloc[i]) > float(close.iloc[i - 1]):
            count += 1
        else:
            break
    return count


def build_trend_context(trade_date: str) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    for item in TRADING_POOL:
        code = item["code"]
        df = _read_price(code, trade_date)
        features = _calc_short_race_features(df)
        if not features or df is None:
            continue
        context[code] = {
            **features,
            "consecutive_up_days": _consecutive_up_days(df),
        }
    return context


def build_historical_signal(
    trade_date: str,
    *,
    cutoff_time: str = "09:30",
    lookback_hours: int = 60,
    channels: set[str] | None = None,
    save: bool = True,
    out_dir: Path | None = None,
    tag: str = "",
) -> dict[str, Any]:
    from datetime import timedelta

    articles = query_articles_before(
        trade_date,
        cutoff_time=cutoff_time,
        lookback_hours=lookback_hours,
        channels=channels,
    )

    # ── 时间切割：上一交易日 15:00 后 = 新鲜 ──
    from news_time_split import split_articles_by_post_close

    fresh_articles, stale_articles, _ = split_articles_by_post_close(articles, trade_date)

    trend_ctx = build_trend_context(trade_date)
    pool_codes = [str(item["code"]).zfill(6) for item in TRADING_POOL]

    # ── 关键词筛选（与实盘一致）──
    fresh_sig = build_news_signal(fresh_articles, trend_context=trend_ctx, date=trade_date)
    stale_sig = build_news_signal(stale_articles, trend_context=trend_ctx, date=trade_date)

    # ── LLM 语义评分（与实盘 _process_news_pool 一致）──
    # 离线回测（ALLOW_NETWORK=0）默认跳过；实盘设 SKIP_NEWS_LLM=1 也可关闭
    allow_news_llm = (
        os.environ.get("ETF_AGENT_ALLOW_NETWORK", "1").strip() == "1"
        and os.environ.get("ETF_AGENT_SKIP_NEWS_LLM", "0").strip() != "1"
    )
    skip_news_llm = not allow_news_llm
    for sig, label in [(fresh_sig, "FRESH"), (stale_sig, "STALE")]:
        accepted = sig.get("accepted_articles", [])
        if skip_news_llm or not accepted or not pool_codes:
            continue
        try:
            llm_results = score_news_with_llm(accepted, pool_codes)
            if llm_results:
                merged = merge_llm_into_news_signal(sig, llm_results)
                if label == "FRESH":
                    fresh_sig = merged
                else:
                    stale_sig = merged
        except Exception as exc:
            print(f"  [historical_news_builder] {trade_date} {label} LLM语义评分异常: {exc}，保留关键词评分。")

    fs, ss = fresh_sig.get("theme_scores", {}), stale_sig.get("theme_scores", {})

    signal = {
        "date": trade_date,
        "source": "split_fresh_stale_historical",
        "cutoff_time": cutoff_time,
        "lookback_hours": lookback_hours,
        "channels": sorted(channels) if channels else "ALL",
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fresh_theme_scores": fs,
        "stale_theme_scores": ss,
        "fresh_accepted_count": fresh_sig.get("accepted_count", 0),
        "stale_accepted_count": stale_sig.get("accepted_count", 0),
        "fresh_strong_count": fresh_sig.get("strong_count", 0),
        "stale_strong_count": stale_sig.get("strong_count", 0),
        "theme_scores": fs,
        "scores": fs,
        "accepted_count": fresh_sig.get("accepted_count", 0) + stale_sig.get("accepted_count", 0),
        "strong_count": fresh_sig.get("strong_count", 0) + stale_sig.get("strong_count", 0),
        "accepted_articles": fresh_sig.get("accepted_articles", []) + stale_sig.get("accepted_articles", []),
        "article_count": len(articles),
        "confidence": fresh_sig.get("confidence", 0.0),
        "market_sentiment": fresh_sig.get("market_sentiment", 0.0),
        "max_abs_theme": fresh_sig.get("max_abs_theme", 0.0),
        "hot_keywords": fresh_sig.get("hot_keywords", []),
        "fresh_accepted_articles": fresh_sig.get("accepted_articles", []),
        "stale_accepted_articles": stale_sig.get("accepted_articles", []),
        "rejected_count": len(articles) - fresh_sig.get("accepted_count", 0) - stale_sig.get("accepted_count", 0),
        "auto_news": {
            "enabled": True, "article_count": len(articles),
            "confidence": fresh_sig.get("confidence", 0.0),
            "market_sentiment": fresh_sig.get("market_sentiment", 0.0),
            "catalyst_hits": fresh_sig.get("catalyst_hits", 0),
            "max_abs_theme": fresh_sig.get("max_abs_theme", 0.0),
        },
        "raw_articles": articles[:120],
    }

    if save:
        out = out_dir or OUT_DIR
        out.mkdir(parents=True, exist_ok=True)
        fname = f"{trade_date}_{tag}.json" if tag else f"{trade_date}.json"
        path = out / fname
        path.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
        signal["path"] = str(path)
    return signal


def main() -> int:
    parser = argparse.ArgumentParser(description="Build one historical news signal from SQLite.")
    parser.add_argument("--date", required=True, help="Trade date, e.g. 2026-05-21")
    parser.add_argument("--cutoff", default="09:30")
    parser.add_argument("--lookback-hours", type=int, default=60)
    args = parser.parse_args()

    signal = build_historical_signal(
        args.date,
        cutoff_time=args.cutoff,
        lookback_hours=args.lookback_hours,
        save=True,
    )
    print(json.dumps({
        "date": signal["date"],
        "article_count": signal["article_count"],
        "accepted_count": signal["accepted_count"],
        "rejected_count": signal["rejected_count"],
        "confidence": signal["confidence"],
        "theme_scores": signal["theme_scores"],
        "path": signal.get("path"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
