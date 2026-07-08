"""ETF 仓位风控模块（从 strategy.py 拆出）。

提供：
  - evaluate_market_regime：宽基 ETF 市场环境判断
  - adjust_invest_ratio_by_news：新闻情绪仓位调整
  - short_race_max_positions：动态持仓数
  - allocate_short_race：集中持有 1-3 只强势 ETF
"""

from __future__ import annotations

import numpy as np
from typing import Any

from features import _get_price_for_decision, _calc_short_race_features
from scoring import SCORE_GATE, RACE_MAX_POSITIONS, RACE_BASE_WEIGHTS, MAX_SINGLE_WEIGHT
from stability_risk import stable_mode_enabled

# ── 本模块独有常量 ──
MIN_AMOUNT = 1000
STABLE_DEFAULT_CAP = 0.55
STABLE_STRONG_CAP = 0.70
STABLE_WEAK_SIGNAL_CAP = 0.35
STABLE_LOSS_CAP = 0.35
STABLE_DRAWDOWN_CAP = 0.25



def evaluate_market_regime(date_str=None):
    """用宽基 ETF 判断整体市场环境，决定是否需要降仓。"""
    refs = ["510300", "159915", "588000"]
    scores = []
    details = []

    for code in refs:
        df = _get_price_for_decision(code, date_str)
        features = _calc_short_race_features(df)
        if not features:
            continue
        score = features["ret_5d"] + features["ret_3d"] * 0.5
        scores.append(score)
        details.append(f"{code}:5日{features['ret_5d']:+.2f}%")

    if not scores:
        return 0.80, "市场环境数据不足，采用80%仓位"

    avg_score = float(np.mean(scores))
    # 弱市保留 15% 试探仓：曾按「开→收比赛口径」测试改为直接空仓，
    # 43 日全链路回测收益反而从 +2.61% 降至 +0.80%（被空掉的弱势日
    # 合计为正收益），故维持试探仓设计。
    if avg_score <= -5.0:
        return 0.15, f"宽基极端走弱({'; '.join(details)})，15%试探仓(利用反弹概率)"
    # 市场评估阈值
    if avg_score <= -2.0:
        return 0.15, f"宽基短期走弱({'; '.join(details)})，仅 15% 试探仓"
    if avg_score <= -0.5:
        return 0.40, f"宽基偏弱({'; '.join(details)})，降至 40% 仓位"
    if avg_score >= 2.0:
        return 0.90, f"市场风险偏好较强({'; '.join(details)})，90%仓位进攻"
    if avg_score >= 0.5:
        return 0.85, f"市场偏强({'; '.join(details)})，85% 仓位"
    return 0.70, f"市场中性({'; '.join(details)})，70% 仓位"


def adjust_invest_ratio_by_news(invest_ratio, market_reason, theme_signals):
    """无清晰主线 → 降仓；低置信/无催化 → 缩量；强情绪小幅加减。"""
    auto_news = theme_signals.get("auto_news", {}) if isinstance(theme_signals, dict) else {}
    confidence = float(auto_news.get("confidence", 0.0) or 0.0)
    sentiment = float(auto_news.get("market_sentiment", 0.0) or 0.0)
    enabled = bool(auto_news.get("enabled", True))
    articles = int(auto_news.get("article_count", 0) or 0)
    catalysts = int(auto_news.get("catalyst_hits", 0) or 0)
    max_abs = float(auto_news.get("max_abs_theme", 0.0) or 0.0)

    # 无东方财富新闻数据时跳过新闻仓位调整（CSDN/纯K线回测兼容）
    if auto_news.get("_skip_news_adjust", False) or (articles == 0 and confidence == 0 and max_abs == 0):
        return invest_ratio, market_reason

    notes = []
    if enabled:
        mult = 1.0
        if articles == 0:
            mult *= 0.55
            notes.append("无新闻")
        if confidence < 0.19:
            mult *= 0.62
            notes.append(f"低置信({confidence:.2f})")
        if max_abs < 0.085:
            mult *= 0.65
            notes.append(f"无主线(max_abs={max_abs:.2f})")
        if articles >= 4 and catalysts == 0 and max_abs < 0.11:
            mult *= 0.62
            notes.append("无催化")
        if mult < 1.0:
            invest_ratio = float(np.clip(invest_ratio * mult, 0.0, 1.0))
            market_reason = f"{market_reason}；{', '.join(notes)} → 仓位 {invest_ratio:.0%}"

    if confidence < 0.24 or abs(sentiment) < 0.18:
        return invest_ratio, market_reason

    if sentiment <= -0.45:
        delta = -0.15
        label = "高可信新闻情绪偏弱"
    elif sentiment <= -0.20:
        delta = -0.08
        label = "新闻情绪略偏谨慎"
    elif sentiment >= 0.45:
        delta = 0.08
        label = "高可信新闻情绪偏积极"
    else:
        delta = 0.04
        label = "新闻情绪略偏积极"

    adjusted = float(np.clip(invest_ratio + delta, 0.0, 1.0))
    if invest_ratio == 0.0:
        return invest_ratio, market_reason  # 市场评估空仓 → 新闻情绪不可复活
    return adjusted, f"{market_reason}；{label}(sentiment={sentiment:+.2f}, confidence={confidence:.2f})，仓位调整至{adjusted:.0%}"


def short_race_max_positions(theme_signals):
    """根据 auto_news 信号强度决定持仓数（1/2/3），与回测口径一致。"""
    auto_news = theme_signals.get("auto_news", {}) if isinstance(theme_signals, dict) else {}
    confidence = float(auto_news.get("confidence", 0.0) or 0.0)
    articles = int(auto_news.get("article_count", 0) or 0)
    max_abs = float(auto_news.get("max_abs_theme", 0.0) or 0.0)
    if articles == 0 or confidence < 0.17 or max_abs < 0.095:
        return 1
    if confidence < 0.26 or max_abs < 0.17:
        return 2
    return RACE_MAX_POSITIONS


def apply_stability_overlay(
    invest_ratio: float,
    market_reason: str,
    ranked: list[dict[str, Any]],
    theme_signals: dict[str, Any],
    *,
    recent_risk: dict[str, Any] | None = None,
) -> tuple[float, str, int | None, dict[str, Any] | None]:
    """Apply conservative 10-day race caps.

    Returns ``(new_ratio, new_reason, max_positions_cap, audit)``. The cap only
    reduces exposure and never revives a cash signal.
    """
    if not stable_mode_enabled() or invest_ratio <= 0:
        return invest_ratio, market_reason, None, None

    recent_risk = recent_risk or {}
    top = ranked[0] if ranked else {}
    second = ranked[1] if len(ranked) > 1 else {}
    top_score = float(top.get("score") or 0.0)
    score_gap = top_score - float(second.get("score") or top_score)

    auto_news = theme_signals.get("auto_news", {}) if isinstance(theme_signals, dict) else {}
    confidence = float(auto_news.get("confidence", 0.0) or 0.0)
    max_abs = float(auto_news.get("max_abs_theme", 0.0) or 0.0)
    articles = int(auto_news.get("article_count", 0) or 0)

    strong_setup = (
        top_score >= 58.0
        and confidence >= 0.26
        and max_abs >= 0.17
        and articles >= 5
    )
    cap = STABLE_STRONG_CAP if strong_setup else STABLE_DEFAULT_CAP
    max_positions_cap = 2 if strong_setup else 1
    notes = [f"稳健模式基础上限{cap:.0%}"]

    weak_signal = top_score < 54.0 or confidence < 0.20 or max_abs < 0.10
    if weak_signal:
        cap = min(cap, STABLE_WEAK_SIGNAL_CAP)
        max_positions_cap = 1
        notes.append("弱信号/低置信，仅保留小仓试探")

    last_pnl = float(recent_risk.get("last_pnl") or 0.0)
    last5_pnl = float(recent_risk.get("last5_pnl") or 0.0)
    consecutive_losses = int(recent_risk.get("consecutive_losses") or 0)
    if last_pnl < 0:
        cap = min(cap, STABLE_LOSS_CAP)
        max_positions_cap = 1
        notes.append(f"上一日亏损{last_pnl:+.0f}元，降至防守")
    if consecutive_losses >= 2 or last5_pnl < -5000:
        cap = min(cap, STABLE_DRAWDOWN_CAP)
        max_positions_cap = 1
        notes.append(
            f"近况熔断(连续亏损{consecutive_losses}，近5次{last5_pnl:+.0f}元)"
        )

    new_ratio = float(min(invest_ratio, cap))
    if new_ratio < invest_ratio:
        market_reason = (
            f"{market_reason}；十天稳健风控 {invest_ratio:.0%}→{new_ratio:.0%}"
            f"（{'；'.join(notes)}）"
        )
    else:
        market_reason = f"{market_reason}；十天稳健风控通过（{'；'.join(notes)}）"

    audit = {
        "enabled": True,
        "original_invest_ratio": round(float(invest_ratio), 4),
        "final_invest_ratio": round(float(new_ratio), 4),
        "cap": round(float(cap), 4),
        "max_positions_cap": max_positions_cap,
        "strong_setup": strong_setup,
        "weak_signal": weak_signal,
        "top_score": round(top_score, 2),
        "score_gap": round(score_gap, 2),
        "news_confidence": round(confidence, 3),
        "news_max_abs": round(max_abs, 3),
        "recent_risk": recent_risk,
        "notes": notes,
    }
    return new_ratio, market_reason, max_positions_cap, audit


def allocate_short_race(ranked, total_capital, invest_ratio, max_positions=None):
    """集中持有 1-3 只强势 ETF；持仓数随信号强度动态调整。"""
    cap = int(max_positions) if max_positions else RACE_MAX_POSITIONS
    cap = max(1, min(RACE_MAX_POSITIONS, cap))
    selected = ranked[:cap]
    # 空仓优先——任何下游条件触发 invest_ratio<=0，直接返回 cash 模式，
    # 比赛输出会是 []，不再误导日志。
    if not selected or invest_ratio <= 0:
        return {
            "allocations": {},
            "summary": {
                "total_candidates_scored": len(ranked),
                "stocks_held": 0,
                "capital_used": 0,
                "cash_reserve": int(total_capital),
                "utilization_rate": 0.0,
                "held_stocks": [],
                "invest_ratio": invest_ratio,
                "mode": "short_race_cash",
            },
        }

    weights = np.array(RACE_BASE_WEIGHTS[:len(selected)], dtype=float)
    weights = weights / weights.sum()

    # 分数差很明显时进一步偏向第一名。
    if len(selected) >= 2 and selected[0]["score"] - selected[1]["score"] >= 8:
        weights[0] += 0.08
        weights[1:] -= 0.08 / (len(selected) - 1)

    # 单ETF最大仓位限制（防单只暴雷）——始终生效
    # 先clip到上限，然后归一化，但确保每只不超过总资本的MAX_SINGLE_WEIGHT
    weights = np.clip(weights, 0.10, MAX_SINGLE_WEIGHT)
    weights = weights / weights.sum()

    investable = total_capital * invest_ratio
    # 二次硬限：每只实际金额不超过总资本 × MAX_SINGLE_WEIGHT
    max_single_amount = total_capital * MAX_SINGLE_WEIGHT
    allocations = {}
    held = []

    for stock, weight in zip(selected, weights):
        amount = int(investable * float(weight) / 100) * 100
        # 二次硬限——单只不超过总资本×30%
        amount = min(amount, int(max_single_amount / 100) * 100)
        if amount < MIN_AMOUNT:
            continue
        code = stock["code"]
        allocations[code] = amount
        held.append({
            "code": code,
            "name": stock["name"],
            "amount": amount,
            "weight": round(amount / total_capital * 100, 1),
            "target_weight": round(float(weight) * invest_ratio * 100, 1),
            "type": "short_race",
            "score": stock["score"],
            "historical_score": stock["historical_score"],
            "trend_score": stock["trend_score"],
            "fresh_theme_score": stock.get("fresh_theme_score", stock.get("theme_score", 0)),
            "stale_theme_score": stock.get("stale_theme_score", 0),
            "theme_score": stock.get("fresh_theme_score", stock.get("theme_score", 0)),
            "theme_raw": stock.get("fresh_theme_raw", stock.get("theme_raw", 0)),
            "pred_return": stock.get("pred_return"),
            "latest_price": stock.get("latest_price"),
            "ret_1d": stock.get("ret_1d"),
            "ret_3d": stock.get("ret_3d"),
            "ret_5d": stock.get("ret_5d"),
            "volume_ratio": stock.get("volume_ratio"),
            "risk_penalty": stock.get("risk_penalty"),
            "reason": stock.get("theme_reason", ""),
        })

    used = int(sum(allocations.values()))
    return {
        "allocations": allocations,
        "summary": {
            "total_candidates_scored": len(ranked),
            "stocks_held": len(held),
            "capital_used": used,
            "cash_reserve": int(total_capital - used),
            "utilization_rate": round(used / total_capital * 100, 1) if total_capital else 0,
            "held_stocks": held,
            "invest_ratio": invest_ratio,
            "mode": "short_race_theme_rotation",
        },
    }
