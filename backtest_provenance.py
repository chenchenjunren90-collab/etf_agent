"""Point-in-time provenance guards for historical news simulations."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from news_time_split import decision_cutoff, split_articles_by_post_close

TOPK_WEIGHTS = (1.0, 0.5, 0.25)
MARKET_REFS = ("510300", "510050", "510500")


def _article_keys(article: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    url = str(article.get("url") or "").strip()
    if url:
        keys.append(f"url:{url}")
    title = str(article.get("title") or "").strip()
    source = str(article.get("source") or "").strip()
    if title:
        keys.append(f"title:{source}|{title}")
    return keys


def _restore_archived_provenance(
    articles: list[dict[str, Any]],
    raw_articles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore timestamps dropped by older scoring archives without rescoring them."""
    raw_by_key: dict[str, dict[str, Any]] = {}
    for raw in raw_articles:
        for key in _article_keys(raw):
            raw_by_key.setdefault(key, raw)

    restored: list[dict[str, Any]] = []
    for article in articles:
        item = dict(article)
        raw = next(
            (raw_by_key[key] for key in _article_keys(item) if key in raw_by_key),
            None,
        )
        if raw:
            for field in ("published_at", "fetched_at"):
                if not str(item.get(field) or "").strip():
                    item[field] = str(raw.get(field) or "")
        restored.append(item)
    return restored


def _parse_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _known_before_cutoff(
    article: dict[str, Any],
    cutoff: datetime,
) -> tuple[bool, str]:
    published = _parse_timestamp(article.get("published_at"))
    if published is None:
        return False, "missing_published_at"
    if published > cutoff:
        return False, "published_after_decision_cutoff"
    fetched_raw = str(article.get("fetched_at") or "").strip()
    if fetched_raw:
        fetched = _parse_timestamp(fetched_raw)
        if fetched is None:
            return False, "invalid_fetched_at"
        if fetched > cutoff:
            return False, "fetched_after_decision_cutoff"
    return True, "accepted"


def _aggregate(articles: list[dict[str, Any]]) -> dict[str, float]:
    contributions: dict[str, list[float]] = {}
    for article in articles:
        for raw_code, raw_value in (article.get("theme_scores") or {}).items():
            code = str(raw_code).zfill(6)
            contributions.setdefault(code, []).append(float(raw_value))
    result: dict[str, float] = {}
    for code, values in contributions.items():
        strongest = sorted(values, key=abs, reverse=True)[: len(TOPK_WEIGHTS)]
        value = sum(item * weight for item, weight in zip(strongest, TOPK_WEIGHTS))
        value = max(-0.85, min(0.85, value))
        if abs(value) >= 0.08:
            result[code] = round(float(value), 3)
    return result


def sanitize_news_signal_for_backtest(
    signal: dict[str, Any],
    trade_date: str,
    *,
    cutoff_time: str | None = None,
) -> dict[str, Any]:
    """Rebuild a news signal using only articles provably known at decision time."""
    cutoff_label = cutoff_time or str(signal.get("cutoff_time") or "09:30")
    cutoff = decision_cutoff(trade_date, cutoff_label)
    source_articles = list(signal.get("fresh_accepted_articles") or [])
    source_articles.extend(signal.get("stale_accepted_articles") or [])
    if not source_articles:
        source_articles = list(signal.get("accepted_articles") or [])
    source_articles = _restore_archived_provenance(
        source_articles,
        list(signal.get("raw_articles") or []),
    )

    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected: dict[str, int] = {}
    for article in source_articles:
        key = str(
            article.get("content_sha256")
            or article.get("url")
            or f"{article.get('published_at')}|{article.get('title')}"
        )
        if key in seen:
            continue
        seen.add(key)
        accepted, reason = _known_before_cutoff(article, cutoff)
        if not accepted:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        unique.append(article)

    fresh, stale, _ = split_articles_by_post_close(unique, trade_date)
    fresh_scores = _aggregate(fresh)
    stale_scores = _aggregate(stale)
    strong_count = sum(1 for article in fresh if article.get("quality") == "strong")
    weak_count = len(fresh) - strong_count
    confidence = min(1.0, 0.20 * strong_count + 0.04 * weak_count)
    market_values = [fresh_scores[code] for code in MARKET_REFS if code in fresh_scores]
    sentiment = sum(market_values) / len(market_values) if market_values else 0.0
    catalyst_hits = sum(
        sum(len(values) for values in (article.get("event_hits") or {}).values())
        for article in fresh
    )
    max_abs = max((abs(value) for value in fresh_scores.values()), default=0.0)

    cleaned = dict(signal)
    cleaned.update({
        "source": "strict_point_in_time_backtest",
        "fresh_theme_scores": fresh_scores,
        "stale_theme_scores": stale_scores,
        "theme_scores": fresh_scores,
        "scores": fresh_scores,
        "fresh_accepted_articles": fresh,
        "stale_accepted_articles": stale,
        "accepted_articles": fresh + stale,
        "fresh_accepted_count": len(fresh),
        "stale_accepted_count": len(stale),
        "accepted_count": len(fresh) + len(stale),
        "strong_count": strong_count,
        "weak_count": weak_count,
        "confidence": round(float(confidence), 3),
        "market_sentiment": round(float(sentiment), 3),
        "max_abs_theme": round(float(max_abs), 3),
        "catalyst_hits": int(catalyst_hits),
        "auto_news": {
            "enabled": True,
            "article_count": len(fresh),
            "confidence": round(float(confidence), 3),
            "market_sentiment": round(float(sentiment), 3),
            "catalyst_hits": int(catalyst_hits),
            "max_abs_theme": round(float(max_abs), 3),
        },
        "backtest_provenance": {
            "decision_cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
            "input_articles": len(source_articles),
            "accepted_articles": len(fresh) + len(stale),
            "rejected_articles": sum(rejected.values()),
            "rejection_reasons": rejected,
        },
    })
    return cleaned
