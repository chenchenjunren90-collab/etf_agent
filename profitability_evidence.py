"""High-precision trade evidence and abstention policy.

The ranking model proposes candidates. This module independently asks whether
similar, strictly prior market states had a positive close-to-close edge after
costs. News may confirm an edge, but it may not create one by itself.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from features import _calc_short_race_features

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

MIN_HISTORY_SAMPLES = 24
NEIGHBOR_COUNT = 36
ROUND_TRIP_COST = 0.0005
HIGH_PROBABILITY = 0.57
CONSERVATIVE_PROBABILITY = 0.53
HIGH_NET_EDGE = 0.0006
CONSERVATIVE_NET_EDGE = 0.0002
HIGH_EXPOSURE_CAP = 0.20
CONSERVATIVE_EXPOSURE_CAP = 0.08

_SAMPLE_CACHE: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}


def _load_price_history(code: str, data_dir: Path) -> pd.DataFrame | None:
    path = data_dir / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path).rename(columns={
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
        })
    except Exception:
        return None
    required = {"date", "close", "volume"}
    if not required.issubset(df.columns):
        return None
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df = df.dropna(subset=["date", "close", "volume"])
    df = df[df["close"] > 0].sort_values("date").drop_duplicates("date", keep="last")
    return df.reset_index(drop=True)


def _feature_vector(features: dict[str, Any]) -> np.ndarray:
    high_break = 1.0 if features.get("high_break") else 0.0
    above_ma = 1.0 if features.get("above_ma") else 0.0
    return np.asarray([
        float(features.get("ret_1d", 0.0) or 0.0) / 2.5,
        float(features.get("ret_3d", 0.0) or 0.0) / 4.0,
        float(features.get("ret_5d", 0.0) or 0.0) / 6.0,
        float(features.get("ret_10d", 0.0) or 0.0) / 10.0,
        math.log(max(0.05, float(features.get("volume_ratio", 1.0) or 1.0))),
        (float(features.get("rsi", 50.0) or 50.0) - 50.0) / 18.0,
        (float(features.get("price_position_20d", 0.5) or 0.5) - 0.5) / 0.30,
        (float(features.get("trend_score", 50.0) or 50.0) - 50.0) / 15.0,
        float(features.get("volatility_20d_pct", 1.5) or 1.5) / 2.0,
        high_break,
        above_ma,
    ], dtype=float)


def _historical_samples(code: str, data_dir: Path) -> list[dict[str, Any]]:
    path = data_dir / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return []
    stat = path.stat()
    key = (str(code).zfill(6), str(data_dir.resolve()), stat.st_mtime_ns, stat.st_size)
    cached = _SAMPLE_CACHE.get(key)
    if cached is not None:
        return cached

    df = _load_price_history(code, data_dir)
    samples: list[dict[str, Any]] = []
    if df is not None:
        for target_index in range(25, len(df)):
            history = df.iloc[:target_index].tail(120).reset_index(drop=True)
            features = _calc_short_race_features(history)
            if not features:
                continue
            previous_close = float(df.iloc[target_index - 1]["close"])
            target_close = float(df.iloc[target_index]["close"])
            if previous_close <= 0:
                continue
            samples.append({
                "date": pd.Timestamp(df.iloc[target_index]["date"]),
                "vector": _feature_vector(features),
                "return": target_close / previous_close - 1.0,
            })
    _SAMPLE_CACHE[key] = samples
    return samples


def estimate_empirical_edge(
    code: str,
    features: dict[str, Any],
    trade_date: str,
    *,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Estimate next-close edge from nearest strictly-prior market states."""
    root = data_dir or DATA_DIR
    target = pd.to_datetime(trade_date, errors="coerce")
    if pd.isna(target):
        return {"available": False, "reason": "invalid_trade_date"}

    samples = [
        item for item in _historical_samples(code, root)
        if item["date"] < target
    ][-180:]
    if len(samples) < MIN_HISTORY_SAMPLES:
        return {
            "available": False,
            "reason": "insufficient_strictly_prior_samples",
            "sample_count": len(samples),
        }

    current = _feature_vector(features)
    distances = np.asarray([
        float(np.sqrt(np.mean(np.square(item["vector"] - current))))
        for item in samples
    ])
    order = np.argsort(distances)[: min(NEIGHBOR_COUNT, len(samples))]
    selected = [samples[int(i)] for i in order]
    selected_distances = distances[order]
    bandwidth = max(0.35, float(np.median(selected_distances)))
    weights = np.exp(-0.5 * np.square(selected_distances / bandwidth))
    weights = np.maximum(weights, 0.05)
    returns = np.asarray([
        float(np.clip(item["return"], -0.04, 0.04))
        for item in selected
    ])
    weight_sum = float(weights.sum())
    effective_n = float(weight_sum * weight_sum / np.square(weights).sum())
    weighted_wins = float(weights[returns > ROUND_TRIP_COST].sum())
    # Weakly informative Beta(3, 3) prior prevents small samples from looking certain.
    positive_probability = (weighted_wins + 3.0) / (weight_sum + 6.0)
    expected_gross = float(np.average(returns, weights=weights))
    variance = float(np.average(np.square(returns - expected_gross), weights=weights))
    standard_error = math.sqrt(max(0.0, variance)) / math.sqrt(max(1.0, effective_n))
    expected_net = expected_gross - ROUND_TRIP_COST
    lower_expected_net = expected_net - standard_error

    return {
        "available": True,
        "sample_count": len(samples),
        "neighbor_count": len(selected),
        "effective_sample_size": round(effective_n, 2),
        "positive_probability": round(float(positive_probability), 4),
        "expected_gross_return": round(expected_gross, 6),
        "expected_net_return": round(expected_net, 6),
        "lower_expected_net": round(lower_expected_net, 6),
        "estimated_cost": ROUND_TRIP_COST,
        "latest_sample_date": max(item["date"] for item in selected).strftime("%Y-%m-%d") if selected else None,
    }


def _direct_news_support(code: str, theme_signals: dict[str, Any]) -> dict[str, Any]:
    articles = (
        theme_signals.get("fresh_accepted_articles")
        or theme_signals.get("accepted_articles")
        or []
    )
    strong = 0
    weak = 0
    titles: list[str] = []
    sources: set[str] = set()
    for article in articles:
        scores = article.get("theme_scores") or {}
        value = float(scores.get(code, 0.0) or 0.0)
        if value <= 0:
            continue
        if str(article.get("quality") or "") == "strong":
            strong += 1
        else:
            weak += 1
        title = str(article.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
        source = str(article.get("source") or "").strip()
        if source:
            sources.add(source)
    return {
        "strong_count": strong,
        "weak_count": weak,
        "source_count": len(sources),
        "titles": titles[:3],
    }


def evaluate_candidate(
    candidate: dict[str, Any],
    theme_signals: dict[str, Any],
    trade_date: str,
    *,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    code = str(candidate.get("code") or "").zfill(6)
    empirical = estimate_empirical_edge(
        code,
        candidate,
        trade_date,
        data_dir=data_dir,
    )
    support = _direct_news_support(code, theme_signals)
    confidence = float(
        theme_signals.get("confidence")
        or (theme_signals.get("auto_news") or {}).get("confidence")
        or 0.0
    )
    fresh_raw = float(candidate.get("fresh_theme_raw", 0.0) or 0.0)
    flags: list[str] = []
    if fresh_raw >= 0.20 and support["strong_count"] + support["weak_count"] == 0:
        flags.append("positive_news_without_direct_event_support")
    if fresh_raw >= 0.20 and confidence < 0.35:
        flags.append("low_news_confidence")
    if float(candidate.get("price_position_20d", 0.5) or 0.5) >= 0.88 and not candidate.get("high_break"):
        flags.append("high_price_position_without_breakout")
    if float(candidate.get("ret_10d", 0.0) or 0.0) >= 8.0 and not candidate.get("high_break"):
        flags.append("late_entry_after_10d_rally")
    if float(candidate.get("rsi", 50.0) or 50.0) >= 72.0:
        flags.append("elevated_rsi")

    action = "cash"
    cap = 0.0
    reason = "empirical_edge_unavailable"
    if empirical.get("available"):
        probability = float(empirical.get("positive_probability") or 0.0)
        expected_net = float(empirical.get("expected_net_return") or 0.0)
        lower_net = float(empirical.get("lower_expected_net") or 0.0)
        hard_news_failure = "positive_news_without_direct_event_support" in flags
        overextended = sum(
            flag in flags
            for flag in ("high_price_position_without_breakout", "late_entry_after_10d_rally", "elevated_rsi")
        )
        high_confirmation = support["strong_count"] > 0 or lower_net > 0.0

        if hard_news_failure:
            reason = "news_event_not_economically_verified"
        elif probability < CONSERVATIVE_PROBABILITY or expected_net < CONSERVATIVE_NET_EDGE:
            reason = "historical_edge_below_conservative_floor"
        elif overextended >= 2:
            reason = "overextended_without_breakout"
        elif (
            probability >= HIGH_PROBABILITY
            and expected_net >= HIGH_NET_EDGE
            and lower_net >= -0.0002
            and high_confirmation
            and not flags
        ):
            action = "trade"
            cap = HIGH_EXPOSURE_CAP
            reason = "high_precision_edge_confirmed"
        elif overextended == 0:
            action = "conservative"
            cap = CONSERVATIVE_EXPOSURE_CAP
            reason = "positive_but_uncertain_edge"
        else:
            reason = "edge_exists_but_entry_risk_is_high"

    return {
        "code": code,
        "action": action,
        "exposure_cap": cap,
        "reason": reason,
        "risk_flags": flags,
        "news_confidence": round(confidence, 3),
        "direct_news_support": support,
        "empirical": empirical,
    }


def evaluate_trade_candidates(
    ranked: list[dict[str, Any]],
    theme_signals: dict[str, Any],
    trade_date: str,
    *,
    data_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return only candidates with a measured edge, plus a full audit trail."""
    audited: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for candidate in ranked:
        item = dict(candidate)
        evidence = evaluate_candidate(item, theme_signals, trade_date, data_dir=data_dir)
        item["profitability_evidence"] = evidence
        audited.append({
            "code": item.get("code"),
            "name": item.get("name"),
            "score": item.get("score"),
            **evidence,
        })
        if evidence["action"] != "cash":
            eligible.append(item)

    priority = {"trade": 2, "conservative": 1, "cash": 0}
    eligible.sort(
        key=lambda item: (
            priority[(item.get("profitability_evidence") or {}).get("action", "cash")],
            float((item.get("profitability_evidence") or {}).get("empirical", {}).get("positive_probability") or 0.0),
            float(item.get("score") or 0.0),
        ),
        reverse=True,
    )
    top_evidence = eligible[0]["profitability_evidence"] if eligible else None
    cap = float(top_evidence.get("exposure_cap") or 0.0) if top_evidence else 0.0
    mode = str(top_evidence.get("action") or "cash") if top_evidence else "cash"
    audit = {
        "version": "profitability-evidence-v3",
        "mode": mode,
        "exposure_cap": cap,
        "max_positions": 1 if eligible else 0,
        "eligible_count": len(eligible),
        "rejected_count": len(ranked) - len(eligible),
        "selected_code": eligible[0].get("code") if eligible else None,
        "candidates": audited,
        "notes": (
            "仅在严格历史相似状态显示成本后正优势时交易；证据不足默认空仓。"
            if not eligible
            else "高置信证据正常仓上限20%；不确定正优势仅保守仓上限8%。"
        ),
    }
    return eligible, audit
