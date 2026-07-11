"""Read the daily strict-news signal for ``strategy.py``."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
SIGNAL_DIR = BASE_DIR / "data" / "daily_news_signal"
ARCHIVE_DIR = SIGNAL_DIR / "archive"
AUTO_SIGNAL_PATH = BASE_DIR / "auto_theme_signal.json"


def _norm_date(date_str: str | None) -> str:
    if date_str:
        return str(date_str)[:10]
    return datetime.now().strftime("%Y-%m-%d")


def signal_path(date_str: str | None = None) -> Path:
    return SIGNAL_DIR / f"{_norm_date(date_str)}.json"


def save_theme_signal(signal: dict[str, Any], date_str: str | None = None) -> Path:
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = signal_path(date_str or signal.get("date"))
    text = json.dumps(signal, ensure_ascii=False, indent=2)
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = ARCHIVE_DIR / f"{path.stem}_{stamp}{path.suffix}"
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(text, encoding="utf-8")
    AUTO_SIGNAL_PATH.write_text(text, encoding="utf-8")
    return path


def _load_signal(date_str: str | None = None) -> dict[str, Any]:
    path = signal_path(date_str)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if AUTO_SIGNAL_PATH.exists():
        return json.loads(AUTO_SIGNAL_PATH.read_text(encoding="utf-8"))
    return {}


def _reason_for(code: str, signal: dict[str, Any]) -> str:
    code = str(code).zfill(6)
    for article in signal.get("accepted_articles", []):
        scores = article.get("theme_scores") or {}
        if code in scores:
            title = article.get("title") or "新闻"
            quality = article.get("quality") or "news"
            reason = article.get("reason") or ""
            flags = ",".join(article.get("risk_flags") or [])
            extra = f"; 风险={flags}" if flags else ""
            return f"{quality}: {title} ({reason}{extra})"
    return "未命中高质量新闻，主要依赖量价确认。"


def _norm_score_map(raw: dict[str, Any] | None) -> dict[str, float]:
    if not raw:
        return {}
    return {str(k).zfill(6): float(v) for k, v in raw.items()}


def get_theme_signals(date_str: str | None = None) -> dict[str, Any]:
    signal = _load_signal(date_str)
    nested = signal.get("auto_news") if isinstance(signal.get("auto_news"), dict) else {}
    fresh_scores = _norm_score_map(signal.get("fresh_theme_scores"))
    stale_scores = _norm_score_map(signal.get("stale_theme_scores"))
    scores = _norm_score_map(signal.get("theme_scores")) or fresh_scores
    if not fresh_scores and scores:
        fresh_scores = dict(scores)
    reason_codes = set(scores) | set(fresh_scores) | set(stale_scores)
    reasons = {code: _reason_for(code, signal) for code in reason_codes}
    sentiment = float(
        signal.get("market_sentiment", nested.get("market_sentiment", 0.0)) or 0.0
    )
    if sentiment >= 0.18:
        market_view = "news_positive"
    elif sentiment <= -0.18:
        market_view = "news_negative"
    else:
        market_view = "news_neutral"

    # 仓位档位用「主 fresh 入选数」；0 是合法值，不能用 `or` 回落到 stale 合计
    if "fresh_accepted_count" in signal:
        article_count = int(signal.get("fresh_accepted_count") or 0)
    elif "article_count" in nested:
        article_count = int(nested.get("article_count") or 0)
    else:
        article_count = int(signal.get("accepted_count") or 0)

    if "catalyst_hits" in signal:
        catalyst_hits = int(signal.get("catalyst_hits") or 0)
    elif "catalyst_hits" in nested:
        catalyst_hits = int(nested.get("catalyst_hits") or 0)
    else:
        catalyst_hits = 0

    return {
        "scores": scores,
        "fresh_theme_scores": fresh_scores,
        "stale_theme_scores": stale_scores,
        "reasons": reasons,
        "source": signal.get("source", "strict_news_filter"),
        "updated_at": signal.get("updated_at", ""),
        "market_view": market_view,
        "hot_keywords": signal.get("hot_keywords") or nested.get("hot_keywords") or [],
        "auto_news": {
            "enabled": True,
            "confidence": float(
                signal.get("confidence", nested.get("confidence", 0.0)) or 0.0
            ),
            "market_sentiment": sentiment,
            "article_count": article_count,
            "raw_article_count": int(signal.get("article_count", 0) or 0),
            "catalyst_hits": catalyst_hits,
            "max_abs_theme": float(
                signal.get("max_abs_theme", nested.get("max_abs_theme", 0.0)) or 0.0
            ),
            "accepted_count": int(signal.get("accepted_count", 0) or 0),
            "rejected_count": int(signal.get("rejected_count", 0) or 0),
            "strong_count": int(signal.get("strong_count", 0) or 0),
            "weak_count": int(signal.get("weak_count", 0) or 0),
        },
    }


def get_theme_reason(code: str, theme_signals: dict[str, Any] | None = None) -> str:
    signals = theme_signals or get_theme_signals()
    return (signals.get("reasons") or {}).get(str(code).zfill(6), "未命中高质量新闻。")
