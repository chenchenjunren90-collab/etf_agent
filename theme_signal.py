"""Read the daily strict-news signal for ``strategy.py``."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
SIGNAL_DIR = BASE_DIR / "data" / "daily_news_signal"
ARCHIVE_DIR = SIGNAL_DIR / "archive"
SNAPSHOT_DIR = SIGNAL_DIR / "snapshots"
AUTO_SIGNAL_PATH = BASE_DIR / "auto_theme_signal.json"


def _norm_date(date_str: str | None) -> str:
    if date_str:
        return str(date_str)[:10]
    return datetime.now().strftime("%Y-%m-%d")


def signal_path(date_str: str | None = None) -> Path:
    return SIGNAL_DIR / f"{_norm_date(date_str)}.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def write_immutable_news_snapshot(
    signal: dict[str, Any],
    date_str: str | None = None,
    *,
    snapshot_dir: Path = SNAPSHOT_DIR,
) -> dict[str, Any]:
    """Store a content-addressed news snapshot without overwriting earlier runs."""
    normalized_date = _norm_date(date_str or signal.get("date"))
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    payload_text = json.dumps(
        signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    document = {
        "captured_at": captured_at,
        "trade_date": normalized_date,
        "sha256": digest,
        "raw_article_count": len(signal.get("raw_articles") or []),
        "signal": signal,
    }
    day_dir = snapshot_dir / normalized_date
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{digest}.json"
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        pass
    return {
        "captured_at": captured_at,
        "sha256": digest,
        "raw_article_count": document["raw_article_count"],
        "path": str(path),
    }


def save_theme_signal(signal: dict[str, Any], date_str: str | None = None) -> Path:
    SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = signal_path(date_str or signal.get("date"))
    text = json.dumps(signal, ensure_ascii=False, indent=2)
    write_immutable_news_snapshot(signal, date_str or signal.get("date"))
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = ARCHIVE_DIR / f"{path.stem}_{stamp}{path.suffix}"
        _atomic_write(backup, path.read_text(encoding="utf-8"))
    _atomic_write(path, text)
    _atomic_write(AUTO_SIGNAL_PATH, text)
    return path


def _load_signal(date_str: str | None = None) -> dict[str, Any]:
    path = signal_path(date_str)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    # 只有实时未指定日期时可回退最新信号；显式历史日期缺档必须返回空，
    # 否则回测会把 auto_theme_signal 的未来新闻注入过去。
    if date_str is None and AUTO_SIGNAL_PATH.exists():
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
        "fresh_accepted_articles": list(signal.get("fresh_accepted_articles") or []),
        "stale_accepted_articles": list(signal.get("stale_accepted_articles") or []),
        "accepted_articles": list(signal.get("accepted_articles") or []),
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
