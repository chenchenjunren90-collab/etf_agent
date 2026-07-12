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

# 单票硬顶分档（稳中求盈利）：高仓少见、弱市低仓、空仓由闸门/极端弱市负责。
# allocate 实际动用 ≈ min(invest_ratio, cap, n_pos × max_single)。
STABLE_MAX_SINGLE_STRONG = 0.45   # 强信号：2×45% 受 70% cap → 可到高仓
STABLE_MAX_SINGLE_MODERATE = 0.30 # 中等：2×30% 受 55% cap → 中高仓
STABLE_MAX_SINGLE_DEFAULT = 0.35  # 常态 1 仓：约 35% 中低仓（多数交易日）
STABLE_MAX_SINGLE_WEAK = 0.25     # 弱信号/连亏：明确低仓

# 【2026-07 复核】elite 门槛(top_score>=58 & confidence>=0.26 & max_abs>=0.17
# & articles>=5)在 86 天回测里只有约 20% 的交易日能达到，其余约 80% 的日子
# 只能持 1 只 ETF。而 allocate_short_race 的单 ETF 硬顶 MAX_SINGLE_WEIGHT=30%
# 与仓位比例是两套独立限制、按 min() 生效——只持 1 只时，无论 invest_ratio
# 定多高（哪怕 98%），实际能用的资金也被硬顶死在 30%。也就是说 STABLE_
# DEFAULT_CAP(55%)/STABLE_WEAK_SIGNAL_CAP(35%) 这两个"仓位比例"上限在只
# 持 1 只时其实从未真正生效过，真正卡住收益的是"只给 1 个仓位名额"本身。
# 实测（stable_backtest.py，2026-03-02~2026-07-06，86天）：把"解锁 2 仓"
# 的门槛从 elite 降到中等信号(MODERATE_*)，2 只 ETF 各 30% 硬顶叠加后上限
# 变为 60%：
#   总收益 +4.89%→+7.51%，Sharpe 1.61→2.20，
#   最大回撤 -3.27%→-3.61%（仍远小于完全不设稳健层的 -6.24%），
#   近10日收益 -0.38%→+0.05%，胜率 40.7%→43.0%。
# 多个邻近阈值组合(top_score 54~56/confidence 0.18~0.24/max_abs 0.10~0.15/
# articles 2~4)结果一致(+7.3%~+7.5%，Sharpe 2.0~2.2)，非偶然拟合单一数字。
# 近况亏损/连续亏损触发的收仓到 1 只（STABLE_LOSS_CAP/STABLE_DRAWDOWN_CAP
# 对应的场景）不受此项影响，仍然强制收紧到 1 只——这项改动只解决"信号
# 还不错但没到 elite 门槛"时被误伤锁死在 1 仓的问题。
MODERATE_SCORE = 55.0
MODERATE_CONFIDENCE = 0.22
MODERATE_MAX_ABS = 0.14
MODERATE_ARTICLES = 4



def evaluate_market_regime(date_str=None):
    """用宽基 ETF 判断整体市场环境，决定是否需要降仓。

    参照与 scoring.market_avg_score 一致：仅用稳健池宽基，避免进攻成长
    （159915/588000）半截 K / 高波动污染仓位比例。
    """
    refs = ["510300", "510050", "510500"]
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
    # 中等信号也解锁 2 仓位（见模块顶部注释）：只要求比 elite 门槛低一档，
    # 不改变仓位比例上限(cap 仍是 STABLE_DEFAULT_CAP)，只是不再把新闻
    # 信号"还不错但没到 elite"的日子锁死在 1 只 ETF（进而被 MAX_SINGLE_
    # WEIGHT=30% 硬顶死），让 2 只 ETF 各 30% 硬顶叠加后能用到 60% 上限。
    moderate_setup = (
        top_score >= MODERATE_SCORE
        and confidence >= MODERATE_CONFIDENCE
        and max_abs >= MODERATE_MAX_ABS
        and articles >= MODERATE_ARTICLES
    )
    cap = STABLE_STRONG_CAP if strong_setup else STABLE_DEFAULT_CAP
    max_positions_cap = 2 if (strong_setup or moderate_setup) else 1
    notes: list[str] = [f"稳健模式基础上限{cap:.0%}"]

    weak_signal = top_score < 54.0 or confidence < 0.20 or max_abs < 0.10
    # 中等信号与弱信号互斥：分数已达中等门槛时不应再标弱信号并锁 1 仓。
    if moderate_setup and top_score >= MODERATE_SCORE:
        weak_signal = False

    if moderate_setup and not strong_setup:
        notes.append("中等信号解锁2仓")
    elif weak_signal:
        max_positions_cap = 1
        cap = min(cap, STABLE_WEAK_SIGNAL_CAP)
        notes.append("弱信号/低置信，仅保留小仓试探")

    last_pnl = float(recent_risk.get("last_pnl") or 0.0)
    last5_pnl = float(recent_risk.get("last5_pnl") or 0.0)
    consecutive_losses = int(recent_risk.get("consecutive_losses") or 0)

    if last_pnl < 0:
        cap = min(cap, STABLE_LOSS_CAP)
        if weak_signal:
            max_positions_cap = 1
        notes.append(f"上一日亏损{last_pnl:+.0f}元，降至防守")

    # 近况回撤：优先压仓位比例；仅在连续亏损或弱信号时才强制单仓。
    if consecutive_losses >= 2:
        cap = min(cap, STABLE_DRAWDOWN_CAP)
        max_positions_cap = 1
        notes.append(
            f"连续亏损{consecutive_losses}次，降至{STABLE_DRAWDOWN_CAP:.0%}且仅1仓"
        )
    elif last5_pnl < -5000:
        cap = min(cap, STABLE_DRAWDOWN_CAP)
        if weak_signal:
            max_positions_cap = 1
            notes.append(
                f"近5次合计{last5_pnl:+.0f}元且信号偏弱，降至{STABLE_DRAWDOWN_CAP:.0%}且仅1仓"
            )
        else:
            notes.append(
                f"近5次合计{last5_pnl:+.0f}元，降至{STABLE_DRAWDOWN_CAP:.0%}（保留多仓资格）"
            )

    new_ratio = float(min(invest_ratio, cap))

    # 单票硬顶与档位对齐，避免「invest_ratio=55% 却只能用 30%」的假高仓，
    # 同时让强信号日真正摸到高仓、弱信号日明确低仓。
    defensive = bool(
        weak_signal
        or consecutive_losses >= 2
        or (last5_pnl < -5000 and weak_signal)
        or last_pnl < 0 and weak_signal
    )
    if strong_setup and max_positions_cap >= 2 and not defensive:
        max_single = STABLE_MAX_SINGLE_STRONG
        notes.append(f"强信号单票上限{max_single:.0%}")
    elif moderate_setup and max_positions_cap >= 2 and not defensive:
        max_single = STABLE_MAX_SINGLE_MODERATE
        notes.append(f"中等信号单票上限{max_single:.0%}")
    elif defensive or weak_signal:
        max_single = min(STABLE_MAX_SINGLE_WEAK, cap)
        notes.append(f"防守单票上限{max_single:.0%}")
    else:
        max_single = min(STABLE_MAX_SINGLE_DEFAULT, cap)
        notes.append(f"常态单票上限{max_single:.0%}")

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
        "max_single_weight": round(float(max_single), 4),
        "strong_setup": strong_setup,
        "moderate_setup": moderate_setup,
        "weak_signal": weak_signal,
        "top_score": round(top_score, 2),
        "score_gap": round(score_gap, 2),
        "news_confidence": round(confidence, 3),
        "news_max_abs": round(max_abs, 3),
        "recent_risk": recent_risk,
        "notes": notes,
    }
    return new_ratio, market_reason, max_positions_cap, audit


def allocate_short_race(
    ranked,
    total_capital,
    invest_ratio,
    max_positions=None,
    *,
    max_single_weight: float | None = None,
):
    """集中持有 1-3 只强势 ETF；持仓数随信号强度动态调整。

    ``max_single_weight`` 由稳健层按档位传入；未传则用全局 MAX_SINGLE_WEIGHT。
    """
    cap = int(max_positions) if max_positions else RACE_MAX_POSITIONS
    cap = max(1, min(RACE_MAX_POSITIONS, cap))
    max_single = float(max_single_weight) if max_single_weight is not None else float(MAX_SINGLE_WEIGHT)
    max_single = max(0.10, min(0.70, max_single))
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
    # 【2026-07 复核，含一次纠偏】2 仓时 RACE_BASE_WEIGHTS[:2] 归一化后是
    # [0.6, 0.4]，两者都已超过 MAX_SINGLE_WEIGHT(30%)；若像原来那样 clip
    # 后再统一 renormalize 补满到 100%，会把两者都拉回恰好 30%/30%，加不
    # 加这次 tilt 最终权重分毫不差（已验证 np.allclose 为 True）。
    # 现在只对触发 tilt 的日子跳过 renormalize：
    #   - 2 仓场景：0.08 的 tilt 幅度仍不足以让 #2 跌破 30% 硬顶，所以
    #     #1/#2 实际权重依旧相等(各30%)，真正的效果是"不再强行补满到
    #     100%投资比例"——总仓位降到约 60%，多出的部分变成现金，而不是
    #     "top 分到更多"（曾在 commit message 里描述为"tilt 让 top 分到
    #     更多"，实测后订正：2 仓下 tilt 从未真正让权重分化，起作用的是
    #     "大分差日子不再强行凑满仓位"这一附带效果）。
    #   - 3 仓场景：tilt 幅度足够让 #2/#3 都跌破 30%，此时才会出现真正的
    #     权重分化（如 0.30/0.26/0.21）。但 3 仓在稳健模式下几乎不会触发
    #     （见 apply_stability_overlay 的 max_positions_cap 恒 ≤2）。
    # 86天回测该项单独验证：+0.22pp、Sharpe 2.20→2.28、最大回撤/近10日
    # 不变。未触发 tilt 的日子行为完全不变（仍是均衡 renormalize）。
    tilted = len(selected) >= 2 and selected[0]["score"] - selected[1]["score"] >= 8
    if tilted:
        weights[0] += 0.08
        weights[1:] -= 0.08 / (len(selected) - 1)

    # 单ETF最大仓位限制——档位由稳健层传入（强信号可到 45%，弱信号 25%）
    weights = np.clip(weights, 0.10, max_single)
    if not tilted:
        weights = weights / weights.sum()

    investable = total_capital * invest_ratio
    # 二次硬限：每只实际金额不超过总资本 × max_single
    max_single_amount = total_capital * max_single
    allocations = {}
    held = []
    execution_dropped: list[dict[str, Any]] = []

    for stock, weight in zip(selected, weights):
        requested_amount = int(investable * float(weight) / 100) * 100
        # 二次硬限——单只不超过总资本×max_single
        requested_amount = min(requested_amount, int(max_single_amount / 100) * 100)
        if requested_amount < MIN_AMOUNT:
            execution_dropped.append({
                "code": stock.get("code"),
                "reason": "below_min_amount",
                "requested_amount": requested_amount,
            })
            continue
        price = float(stock.get("latest_price") or 0.0)
        if price <= 0:
            execution_dropped.append({
                "code": stock.get("code"),
                "reason": "missing_execution_price",
                "requested_amount": requested_amount,
            })
            continue
        volume = int(requested_amount // price // 100 * 100)
        if volume <= 0:
            execution_dropped.append({
                "code": stock.get("code"),
                "reason": "insufficient_for_one_lot",
                "requested_amount": requested_amount,
                "latest_price": price,
            })
            continue
        amount = round(volume * price, 2)
        code = stock["code"]
        allocations[code] = amount
        held.append({
            "code": code,
            "name": stock["name"],
            "amount": amount,
            "volume": volume,
            "weight": round(amount / total_capital * 100, 1),
            "target_weight": round(float(weight) * invest_ratio * 100, 1),
            "type": "short_race",
            "score": stock["score"],
            "historical_score": stock.get("historical_score", 0),
            "trend_score": stock.get("trend_score", 0),
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
            "execution_dropped": execution_dropped,
        },
    }
