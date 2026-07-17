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
from pool import OFFENSIVE_CODES

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

MIN_HISTORY_SAMPLES = 24
NEIGHBOR_COUNT = 36
ROUND_TRIP_COST = 0.0005
HIGH_PROBABILITY = 0.57
CONSERVATIVE_PROBABILITY = 0.53
HIGH_NET_EDGE = 0.0006
CONSERVATIVE_NET_EDGE = 0.0002
HIGH_EXPOSURE_CAP = 0.12
CONSERVATIVE_EXPOSURE_CAP = 0.08
UNCALIBRATED_EXPOSURE_CAP = 0.05
CALIBRATION_MIN_SIGNALS = 8
CALIBRATION_MAX_ANCHORS = 90
PRICE_ADMISSION_GATE = 50.0
EVENT_PRICE_ADMISSION_GATE = 35.0
EVENT_PROBE_EXPOSURE_CAP = 0.03
EVENT_PROBE_PROBABILITY = 0.57
EVENT_PROBE_MIN_EXPECTED_NET = 0.002
EVENT_PROBE_MIN_LOWER_NET = -0.0015
CADENCE_WINDOW_DAYS = 12
CADENCE_MIN_TRADE_DAYS = 2
CADENCE_MAX_TRADE_DAYS = 4
CADENCE_MIN_CASH_STREAK = 3
CADENCE_PROBE_EXPOSURE_CAP = 0.02
CADENCE_PROBE_PROBABILITY = 0.54
CADENCE_PROBE_MIN_PRICE_SCORE = PRICE_ADMISSION_GATE
CADENCE_PROBE_MIN_EXPECTED_NET = 0.0
CADENCE_PROBE_MIN_LOWER_NET = -0.0025
CADENCE_ABOVE_TARGET_EXPOSURE_CAP = 0.05
PROFITABILITY_EVIDENCE_VERSION = "profitability-evidence-v7-semantic-events"

_SAMPLE_CACHE: dict[tuple[str, str, int, int], list[dict[str, Any]]] = {}
_CALIBRATION_CACHE: dict[tuple[str, str, str, int, int], dict[str, Any]] = {}

BROAD_ETF_CATALYST_HINTS = (
    "行业", "产业", "板块", "政策", "全市场", "多家", "集体", "景气",
    "供需", "价格", "关税", "利率", "流动性", "央行", "证监会",
    "国务院", "工信部", "发改委", "财政部", "交易所",
)
IDIOSYNCRATIC_EVENT_HINTS = (
    "IPO", "上会", "申购", "打新", "中签", "新股", "单家公司",
)


def _is_broad_etf_catalyst(article: dict[str, Any]) -> bool:
    semantic = article.get("semantic_event") or {}
    if semantic.get("grounded") and semantic.get("scope") in {
        "market", "sector", "multi_company",
    }:
        return True
    core = " ".join(
        str(article.get(key) or "")
        for key in ("title", "summary", "digest")
    )
    if any(hint in core for hint in IDIOSYNCRATIC_EVENT_HINTS):
        return False
    return any(hint in core for hint in BROAD_ETF_CATALYST_HINTS)


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


def _neighbor_estimate(
    samples: list[dict[str, Any]],
    current: np.ndarray,
) -> dict[str, Any] | None:
    if len(samples) < MIN_HISTORY_SAMPLES:
        return None
    recent = samples[-180:]
    distances = np.asarray([
        float(np.sqrt(np.mean(np.square(item["vector"] - current))))
        for item in recent
    ])
    order = np.argsort(distances)[: min(NEIGHBOR_COUNT, len(recent))]
    selected = [recent[int(i)] for i in order]
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
    positive_probability = (weighted_wins + 3.0) / (weight_sum + 6.0)
    expected_gross = float(np.average(returns, weights=weights))
    variance = float(np.average(np.square(returns - expected_gross), weights=weights))
    standard_error = math.sqrt(max(0.0, variance)) / math.sqrt(max(1.0, effective_n))
    expected_net = expected_gross - ROUND_TRIP_COST
    return {
        "selected": selected,
        "neighbor_count": len(selected),
        "effective_sample_size": effective_n,
        "positive_probability": float(positive_probability),
        "expected_gross_return": expected_gross,
        "expected_net_return": expected_net,
        "lower_expected_net": expected_net - standard_error,
    }


def _walk_forward_calibration(
    code: str,
    samples: list[dict[str, Any]],
    trade_date: str,
    data_dir: Path,
) -> dict[str, Any]:
    path = data_dir / f"{str(code).zfill(6)}.csv"
    stat = path.stat()
    key = (
        str(code).zfill(6),
        str(data_dir.resolve()),
        str(trade_date)[:10],
        stat.st_mtime_ns,
        stat.st_size,
    )
    cached = _CALIBRATION_CACHE.get(key)
    if cached is not None:
        return cached

    triggered: list[dict[str, Any]] = []
    start = max(MIN_HISTORY_SAMPLES, len(samples) - CALIBRATION_MAX_ANCHORS)
    for index in range(start, len(samples)):
        prediction = _neighbor_estimate(samples[:index], samples[index]["vector"])
        if not prediction:
            continue
        if (
            prediction["positive_probability"] >= CONSERVATIVE_PROBABILITY
            and prediction["expected_net_return"] >= CONSERVATIVE_NET_EDGE
        ):
            realized_net = float(samples[index]["return"]) - ROUND_TRIP_COST
            triggered.append({
                "date": samples[index]["date"],
                "realized_net_return": realized_net,
                "win": realized_net > 0.0,
            })

    count = len(triggered)
    wins = sum(1 for item in triggered if item["win"])
    posterior_win_rate = (wins + 2.0) / (count + 4.0)
    mean_net = float(np.mean([
        item["realized_net_return"] for item in triggered
    ])) if triggered else 0.0
    if count < CALIBRATION_MIN_SIGNALS:
        status = "insufficient"
    elif mean_net > 0.0 and posterior_win_rate >= 0.50:
        status = "positive"
    else:
        status = "negative"
    result = {
        "status": status,
        "signal_count": count,
        "wins": wins,
        "posterior_win_rate": round(float(posterior_win_rate), 4),
        "mean_realized_net_return": round(mean_net, 6),
        "latest_evaluation_date": (
            triggered[-1]["date"].strftime("%Y-%m-%d") if triggered else None
        ),
    }
    _CALIBRATION_CACHE[key] = result
    return result


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

    estimate = _neighbor_estimate(samples, _feature_vector(features))
    if estimate is None:
        return {"available": False, "reason": "neighbor_estimate_unavailable"}
    selected = estimate.pop("selected")
    calibration = _walk_forward_calibration(code, samples, trade_date, root)

    return {
        "available": True,
        "sample_count": len(samples),
        "neighbor_count": estimate["neighbor_count"],
        "effective_sample_size": round(estimate["effective_sample_size"], 2),
        "positive_probability": round(estimate["positive_probability"], 4),
        "expected_gross_return": round(estimate["expected_gross_return"], 6),
        "expected_net_return": round(estimate["expected_net_return"], 6),
        "lower_expected_net": round(estimate["lower_expected_net"], 6),
        "estimated_cost": ROUND_TRIP_COST,
        "latest_sample_date": max(item["date"] for item in selected).strftime("%Y-%m-%d") if selected else None,
        "walk_forward_calibration": calibration,
    }


def _grounded_semantic_support(
    article: dict[str, Any],
    code: str,
) -> tuple[float, str] | None:
    event = article.get("semantic_event") or {}
    if not event.get("grounded"):
        return None
    if event.get("event_status") not in {"occurred", "announced"}:
        return None
    if event.get("novelty") == "repeat":
        return None
    if event.get("scope") not in {"market", "sector", "multi_company"}:
        return None
    for judgment in event.get("etf_judgments") or []:
        if str(judgment.get("code") or "").zfill(6) != code:
            continue
        if not judgment.get("direct_evidence"):
            continue
        direction = str(
            judgment.get("direction") or judgment.get("sentiment") or "neutral"
        )
        if direction not in {"positive", "negative"}:
            continue
        try:
            value = float(
                judgment.get("score")
                or (article.get("semantic_theme_scores") or {}).get(code)
                or 0.0
            )
        except (TypeError, ValueError):
            continue
        if value == 0.0:
            continue
        strength = str(judgment.get("strength") or "weak")
        return value, strength
    return None


def _direct_news_support(code: str, theme_signals: dict[str, Any]) -> dict[str, Any]:
    from news_signal import direct_core_theme_scores

    articles = (
        theme_signals.get("fresh_accepted_articles")
        or theme_signals.get("accepted_articles")
        or []
    )
    strong = 0
    weak = 0
    strong_negative = 0
    weak_negative = 0
    titles: list[str] = []
    negative_titles: list[str] = []
    sources: set[str] = set()
    broad_strong = 0
    broad_sources: set[str] = set()
    discarded_indirect = 0
    discarded_titles: list[str] = []
    semantic_confirmed = 0
    semantic_unconfirmed = 0
    semantic_reviewed = bool(
        theme_signals.get("semantic_review_completed")
        or theme_signals.get("fresh_semantic_review_completed")
    )
    for article in articles:
        article_semantic_reviewed = bool(
            article.get("semantic_reviewed", semantic_reviewed)
        )
        scores = article.get("theme_scores") or {}
        semantic_support = _grounded_semantic_support(article, code)
        if semantic_support is not None:
            value, semantic_strength = semantic_support
            semantic_confirmed += 1
        else:
            value = float(scores.get(code, 0.0) or 0.0)
        if value == 0:
            continue
        # New runs require grounded semantic confirmation. Historical archives
        # predate that layer and retain deterministic core-field validation so
        # point-in-time replays do not silently change their evidence source.
        if article_semantic_reviewed and semantic_support is None:
            semantic_unconfirmed += 1
            discarded_indirect += 1
            title = str(article.get("title") or "").strip()
            if title and title not in discarded_titles:
                discarded_titles.append(title)
            continue
        if not article_semantic_reviewed and code not in direct_core_theme_scores(article):
            discarded_indirect += 1
            title = str(article.get("title") or "").strip()
            if title and title not in discarded_titles:
                discarded_titles.append(title)
            continue
        is_strong = (
            semantic_support is not None and semantic_strength == "strong"
        ) or str(article.get("quality") or "") == "strong"
        if value > 0 and is_strong:
            strong += 1
            if _is_broad_etf_catalyst(article):
                broad_strong += 1
        elif value > 0:
            weak += 1
        elif is_strong:
            strong_negative += 1
        else:
            weak_negative += 1
        title = str(article.get("title") or "").strip()
        if value > 0 and title and title not in titles:
            titles.append(title)
        if value < 0 and title and title not in negative_titles:
            negative_titles.append(title)
        source = str(article.get("source") or "").strip()
        if source:
            sources.add(source)
            if value > 0 and is_strong and _is_broad_etf_catalyst(article):
                broad_sources.add(source)
    return {
        "strong_count": strong,
        "weak_count": weak,
        "strong_negative_count": strong_negative,
        "weak_negative_count": weak_negative,
        "source_count": len(sources),
        "broad_strong_count": broad_strong,
        "broad_source_count": len(broad_sources),
        "discarded_indirect_count": discarded_indirect,
        "discarded_indirect_titles": discarded_titles[:3],
        "semantic_reviewed": semantic_reviewed,
        "semantic_confirmed_count": semantic_confirmed,
        "semantic_unconfirmed_count": semantic_unconfirmed,
        "titles": titles[:3],
        "negative_titles": negative_titles[:3],
    }


def evaluate_candidate(
    candidate: dict[str, Any],
    theme_signals: dict[str, Any],
    trade_date: str,
    *,
    data_dir: Path | None = None,
    recent_submit_history: list[dict[str, Any]] | None = None,
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
    if support["strong_negative_count"] > 0:
        flags.append("direct_strong_negative_news")
    if float(candidate.get("price_position_20d", 0.5) or 0.5) >= 0.88 and not candidate.get("high_break"):
        flags.append("high_price_position_without_breakout")
    if float(candidate.get("ret_10d", 0.0) or 0.0) >= 8.0 and not candidate.get("high_break"):
        flags.append("late_entry_after_10d_rally")
    if float(candidate.get("rsi", 50.0) or 50.0) >= 72.0:
        flags.append("elevated_rsi")
    ret_1d = float(candidate.get("ret_1d", 0.0) or 0.0)
    rsi = float(candidate.get("rsi", 50.0) or 50.0)
    price_position = float(candidate.get("price_position_20d", 0.5) or 0.5)
    high_break = bool(candidate.get("high_break"))
    price_score = float(candidate.get("price_score", candidate.get("score", 0.0)) or 0.0)
    event_rotation_probe = bool(
        code in OFFENSIVE_CODES
        and support["strong_count"] + support["weak_count"] > 0
        and support["broad_strong_count"] > 0
        and confidence >= 0.70
        and price_score >= EVENT_PRICE_ADMISSION_GATE
        and price_score < PRICE_ADMISSION_GATE
    )
    if price_score < PRICE_ADMISSION_GATE and not event_rotation_probe:
        flags.append("news_promoted_without_price_gate")
    elif price_score < PRICE_ADMISSION_GATE:
        flags.append("event_supported_early_rotation")
    if ret_1d >= 2.0 and (
        not high_break or (rsi >= 65.0 and price_position >= 0.95)
    ):
        flags.append("one_day_surge_entry_risk")
    if (
        high_break
        and float(candidate.get("ret_10d", 0.0) or 0.0) >= 10.0
        and float(candidate.get("volume_ratio", 1.0) or 1.0) < 0.8
    ):
        flags.append("low_volume_late_breakout")
    volatility = float(candidate.get("volatility_20d_pct", 0.0) or 0.0)
    ret_3d = float(candidate.get("ret_3d", 0.0) or 0.0)
    if ret_1d <= -3.0 and volatility >= 2.5:
        flags.append("violent_reversal_entry_risk")
    if ret_3d <= -6.0 and not candidate.get("above_ma"):
        flags.append("deep_short_term_downtrend")

    if recent_submit_history:
        try:
            from trading_calendar import previous_trading_day

            previous_date = pd.Timestamp(previous_trading_day(trade_date)).strftime("%Y-%m-%d")
            previous_rows = [
                row for row in recent_submit_history
                if str(row.get("date") or "")[:10] == previous_date
            ]
            if previous_rows:
                symbols = previous_rows[-1].get("symbols") or []
                if isinstance(symbols, str):
                    symbols = [part.strip() for part in symbols.split(",") if part.strip()]
                if code in {str(value).zfill(6) for value in symbols}:
                    flags.append("same_symbol_previous_trade_day")
        except Exception:
            pass

    action = "cash"
    cap = 0.0
    reason = "empirical_edge_unavailable"
    if empirical.get("available"):
        probability = float(empirical.get("positive_probability") or 0.0)
        expected_net = float(empirical.get("expected_net_return") or 0.0)
        lower_net = float(empirical.get("lower_expected_net") or 0.0)
        hard_news_failure = "positive_news_without_direct_event_support" in flags
        entry_veto = next((
            flag for flag in (
                "one_day_surge_entry_risk",
                "low_volume_late_breakout",
                "violent_reversal_entry_risk",
                "deep_short_term_downtrend",
                "news_promoted_without_price_gate",
                "direct_strong_negative_news",
            )
            if flag in flags
        ), None)
        overextended = sum(
            flag in flags
            for flag in ("high_price_position_without_breakout", "late_entry_after_10d_rally", "elevated_rsi")
        )
        high_confirmation = support["strong_count"] > 0 or lower_net > 0.0
        calibration = empirical.get("walk_forward_calibration") or {
            "status": "insufficient",
            "signal_count": 0,
        }

        if hard_news_failure:
            reason = "news_event_not_economically_verified"
        elif entry_veto:
            reason = entry_veto
        elif probability < CONSERVATIVE_PROBABILITY or expected_net < CONSERVATIVE_NET_EDGE:
            reason = "historical_edge_below_conservative_floor"
        elif overextended >= 2:
            reason = "overextended_without_breakout"
        elif calibration.get("status") == "negative":
            reason = "walk_forward_calibration_not_profitable"
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

        if action != "cash" and calibration.get("status") == "insufficient":
            action = "conservative"
            cap = UNCALIBRATED_EXPOSURE_CAP
            reason = "insufficient_calibration_small_trial"
        if action != "cash" and event_rotation_probe:
            event_evidence_is_strong = bool(
                probability >= EVENT_PROBE_PROBABILITY
                and expected_net >= EVENT_PROBE_MIN_EXPECTED_NET
                and lower_net >= EVENT_PROBE_MIN_LOWER_NET
                and calibration.get("status") == "positive"
            )
            if event_evidence_is_strong:
                action = "conservative"
                cap = min(cap, EVENT_PROBE_EXPOSURE_CAP)
                reason = "event_supported_early_rotation_probe"
            else:
                action = "cash"
                cap = 0.0
                reason = "event_rotation_evidence_below_floor"

    return {
        "code": code,
        "action": action,
        "exposure_cap": cap,
        "reason": reason,
        "risk_flags": flags,
        "news_confidence": round(confidence, 3),
        "price_score": round(price_score, 2),
        "score_gate_floor": (
            EVENT_PRICE_ADMISSION_GATE
            if reason == "event_supported_early_rotation_probe"
            else None
        ),
        "direct_news_support": support,
        "empirical": empirical,
    }


def evaluate_trade_candidates(
    ranked: list[dict[str, Any]],
    theme_signals: dict[str, Any],
    trade_date: str,
    *,
    data_dir: Path | None = None,
    recent_submit_history: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return only candidates with a measured edge, plus a full audit trail."""
    audited: list[dict[str, Any]] = []
    evaluated_items: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for candidate in ranked:
        item = dict(candidate)
        evidence = evaluate_candidate(
            item,
            theme_signals,
            trade_date,
            data_dir=data_dir,
            recent_submit_history=recent_submit_history,
        )
        item["profitability_evidence"] = evidence
        evaluated_items.append(item)
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
    recent_rows = list(recent_submit_history or [])[-(CADENCE_WINDOW_DAYS - 1):]
    recent_trade_days = sum(bool(row.get("symbols")) for row in recent_rows)
    cash_streak = 0
    for row in reversed(recent_rows):
        if row.get("symbols"):
            break
        cash_streak += 1
    cadence_upper_target_reached = recent_trade_days >= CADENCE_MAX_TRADE_DAYS
    cadence_probe_code = None
    if (
        not eligible
        and recent_trade_days < CADENCE_MIN_TRADE_DAYS
        and cash_streak >= CADENCE_MIN_CASH_STREAK
    ):
        hard_flags = {
            "positive_news_without_direct_event_support",
            "one_day_surge_entry_risk",
            "low_volume_late_breakout",
            "violent_reversal_entry_risk",
            "deep_short_term_downtrend",
            "news_promoted_without_price_gate",
            "direct_strong_negative_news",
        }
        probe_candidates: list[dict[str, Any]] = []
        for item in evaluated_items:
            evidence = item.get("profitability_evidence") or {}
            empirical = evidence.get("empirical") or {}
            calibration = empirical.get("walk_forward_calibration") or {}
            flags = set(evidence.get("risk_flags") or [])
            overextended = len(flags & {
                "high_price_position_without_breakout",
                "late_entry_after_10d_rally",
                "elevated_rsi",
            })
            calibrated = calibration.get("status") == "positive" or (
                calibration.get("status") == "insufficient"
                and int(calibration.get("signal_count") or 0) >= 5
                and float(calibration.get("posterior_win_rate") or 0.0) >= 0.55
                and float(calibration.get("mean_realized_net_return") or 0.0) > 0.0
            )
            if (
                empirical.get("available")
                and float(empirical.get("positive_probability") or 0.0)
                >= CADENCE_PROBE_PROBABILITY
                and float(empirical.get("expected_net_return") or 0.0)
                >= CADENCE_PROBE_MIN_EXPECTED_NET
                and float(empirical.get("lower_expected_net") or 0.0)
                >= CADENCE_PROBE_MIN_LOWER_NET
                and float(evidence.get("price_score") or 0.0)
                >= CADENCE_PROBE_MIN_PRICE_SCORE
                and calibrated
                and not (flags & hard_flags)
                and overextended < 2
            ):
                probe_candidates.append(item)
        probe_candidates.sort(
            key=lambda item: (
                float((item.get("profitability_evidence") or {}).get("empirical", {}).get("expected_net_return") or 0.0),
                float((item.get("profitability_evidence") or {}).get("empirical", {}).get("positive_probability") or 0.0),
                float(item.get("price_score") or 0.0),
            ),
            reverse=True,
        )
        if probe_candidates:
            selected = probe_candidates[0]
            evidence = dict(selected.get("profitability_evidence") or {})
            evidence.update({
                "action": "conservative",
                "exposure_cap": CADENCE_PROBE_EXPOSURE_CAP,
                "reason": "cadence_positive_edge_probe",
                "cadence_probe": True,
                "score_gate_floor": CADENCE_PROBE_MIN_PRICE_SCORE,
            })
            selected["profitability_evidence"] = evidence
            cadence_probe_code = str(selected.get("code") or "").zfill(6)
            eligible = [selected]
            for audit_item in audited:
                if str(audit_item.get("code") or "").zfill(6) == cadence_probe_code:
                    audit_item.update(evidence)
                    break
    cadence_size_limited = False
    if cadence_upper_target_reached and eligible:
        for item in eligible:
            evidence = dict(item.get("profitability_evidence") or {})
            original_cap = float(evidence.get("exposure_cap") or 0.0)
            limited_cap = min(original_cap, CADENCE_ABOVE_TARGET_EXPOSURE_CAP)
            evidence.update({
                "exposure_cap": limited_cap,
                "cadence_size_limited": limited_cap < original_cap,
                "cadence_original_exposure_cap": original_cap,
            })
            item["profitability_evidence"] = evidence
            cadence_size_limited = cadence_size_limited or limited_cap < original_cap
            for audit_item in audited:
                if str(audit_item.get("code") or "").zfill(6) == str(item.get("code") or "").zfill(6):
                    audit_item.update(evidence)
                    break
    top_evidence = eligible[0]["profitability_evidence"] if eligible else None
    cap = float(top_evidence.get("exposure_cap") or 0.0) if top_evidence else 0.0
    mode = str(top_evidence.get("action") or "cash") if top_evidence else "cash"
    audit = {
        "version": PROFITABILITY_EVIDENCE_VERSION,
        "mode": mode,
        "exposure_cap": cap,
        "max_positions": 1 if eligible else 0,
        "eligible_count": len(eligible),
        "rejected_count": len(ranked) - len(eligible),
        "selected_code": eligible[0].get("code") if eligible else None,
        "score_gate_floor": (
            float(top_evidence.get("score_gate_floor"))
            if top_evidence and top_evidence.get("score_gate_floor") is not None
            else None
        ),
        "cadence": {
            "window_days": CADENCE_WINDOW_DAYS,
            "target_trade_days": [CADENCE_MIN_TRADE_DAYS, CADENCE_MAX_TRADE_DAYS],
            "recent_trade_days": recent_trade_days,
            "projected_trade_days": recent_trade_days + (1 if eligible else 0),
            "cash_streak": cash_streak,
            "shortfall": max(0, CADENCE_MIN_TRADE_DAYS - recent_trade_days),
            "upper_target_reached": cadence_upper_target_reached,
            "hard_blocked": False,
            "size_limited": cadence_size_limited,
            "forced_trade": False,
            "probe_code": cadence_probe_code,
        },
        "candidates": audited,
        "notes": (
            "仅在严格历史相似状态显示成本后正优势时交易；证据不足默认空仓。"
            if not eligible
            else "高置信证据仓位上限12%；保守正优势上限8%；未校准试探上限5%。"
        ),
    }
    return eligible, audit
