"""ETF 评分排名模块（从 strategy.py 拆出）。

提供：
  - rank_etfs_short_race：短赛 ETF 综合评分与排名
  - _inject_llm_views_into_signals：LLM 态度分注入主题信号
  - market_avg_score：候选池平均评分
  - SCORE_GATE / ECON_TIER*_CAP 等关键常量

权重：fresh×25% + stale×10% + trend×30% + hist×20% - risk×15% - rotation
  经济日历分级仓位上限(85/75/65%) + 单ETF硬限30%总资产 + SCORE_GATE=50
"""

from __future__ import annotations

import os
from typing import Any

import numpy as np

from features import _get_price_for_decision, _calc_short_race_features

# ═══════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════
SCORE_GATE = 50.0
RACE_MAX_POSITIONS = 3
RACE_BASE_WEIGHTS = [0.45, 0.30, 0.25]
RACE_MIN_INVEST_RATIO = 0.00
# 经济日历分级仓位上限（根据高影响事件数量动态调整）
ECON_TIER1_CAP = 0.85   # 1-2条高影响
ECON_TIER2_CAP = 0.75   # 3-5条高影响
ECON_TIER3_CAP = 0.65   # 6+条高影响

# 评分闸门动态模式：环境变量 SCORE_GATE_MODE=dynamic 时，
# LLM 强信号(max|score|>=0.5)可将闸门降至 55
SCORE_GATE_DYNAMIC_FLOOR = 42.0

# 轮动惩罚参数
# 2026-07 实测关闭：开→收比赛口径下惩罚"连续强势"是反动量，
# 85 日回测 +5.19%(开) vs +9.11%(关)，两个子窗口均为关闭更优
# （3~4月 +3.43%、5~7月 +5.48%）。追踪器保留仅供审计展示。
ROTATION_MAX_PENALTY = 0.0
ROTATION_RESET_THRESHOLD = 3

MAX_POSITIONS = 6
MAX_SINGLE_WEIGHT = 0.30  # 单ETF最大仓位30%（防单只暴雷）

# 轮动追踪（模块级状态）
_rotation_tracker: dict[str, int] = {}
_rotation_absent: dict[str, int] = {}


def reset_rotation_tracker():
    """重置轮动追踪（回测切换日期时调用）。"""
    global _rotation_tracker, _rotation_absent
    _rotation_tracker = {}
    _rotation_absent = {}


def _get_rotation_penalty(code: str) -> float:
    """轮动惩罚已停用（见 ROTATION_MAX_PENALTY 注释），恒返回 0。"""
    return 0.0


def _update_rotation_tracker(top_codes: list[str]):
    """更新轮动追踪。"""
    global _rotation_tracker, _rotation_absent
    top_set = set(top_codes)
    for code in set(list(_rotation_tracker) + list(_rotation_absent) + list(top_set)):
        if code in top_set:
            _rotation_tracker[code] = _rotation_tracker.get(code, 0) + 1
            _rotation_absent[code] = 0
        else:
            _rotation_absent[code] = _rotation_absent.get(code, 0) + 1
            if _rotation_absent[code] >= ROTATION_RESET_THRESHOLD:
                _rotation_tracker[code] = 0


CASH_THRESHOLD = 0.3
CASH_THRESHOLD_FULL = 0.0


def score_stock(df):
    """
    6因子打分模型 (0-100分):
      动量 25% | RSI 20% | MACD 15% | 量比 15% | 趋势 10% | 布林带 15%

    布林带逻辑:
      - position 接近0（下轨附近）= 超卖，加分
      - position 接近1（上轨附近）= 超买，扣分
      - position 在0.3-0.7区间 = 正常
    """
    if df is None or len(df) < 20:
        return None

    close = df["close"].dropna()
    vol = df["volume"].dropna()
    if len(close) < 20:
        return None

    from indicators import calc_momentum, calc_rsi, calc_macd, calc_volume_ratio, calc_trend_strength, calc_bollinger

    # --- Factor 1: 动量 (5日收益率) ---
    mom = calc_momentum(close, period=5)
    mom_score = float(np.clip(mom * 5 + 50, 0, 100))

    # --- Factor 2: RSI ---
    rsi = float(calc_rsi(close).iloc[-1])
    if rsi > 75:
        rsi_score = float(max(20, 100 - (rsi - 75) * 3))
    elif rsi < 25:    # 30→25
        rsi_score = float(min(80, 25 + (25 - rsi) * 2))
    else:
        rsi_score = float(50 + (50 - abs(rsi - 50)) * 0.8)

    # --- Factor 3: MACD ---
    macd, signal, hist = calc_macd(close)
    if hist > 0:
        macd_score = float(min(100, 60 + min(40, hist / close.iloc[-1] * 1000)))
    else:
        macd_score = float(max(10, 50 + hist / close.iloc[-1] * 1000))

    # --- Factor 4: 量比 ---
    vr = calc_volume_ratio(vol)
    vr_score = float(np.clip(vr * 30, 0, 100))

    # --- Factor 5: 趋势强度 ---
    ts = calc_trend_strength(close)
    ts_score = float(ts * 100)

    # --- Factor 6: 布林带 ---
    bb_pos, bb_width = calc_bollinger(close)
    if bb_pos <= 0.3:
        bb_score = float(np.clip(50 + (0.3 - bb_pos) * 200, 30, 80))
    elif bb_pos >= 0.7:
        bb_score = float(np.clip(70 - (bb_pos - 0.7) * 200, 20, 70))
    else:
        bb_score = 70.0

    # --- 加权总分 ---
    total = (
        mom_score  * 0.25 +
        rsi_score  * 0.25 +
        macd_score * 0.15 +
        vr_score   * 0.15 +
        ts_score   * 0.10 +
        bb_score   * 0.10   # 15%→10%
    )

    return {
        "momentum": round(mom, 2),
        "rsi": round(rsi, 1),
        "macd": round(macd, 3),
        "macd_hist": round(hist, 3),
        "volume_ratio": round(vr, 2),
        "trend_strength": round(ts, 2),
        "bollinger_position": round(bb_pos, 3),
        "bollinger_width": round(bb_width, 4),
        "score": round(total, 1),
        "latest_price": float(close.iloc[-1]),
    }


def rank_etfs_short_race(pool, date_str=None):
    """短赛 ETF 排序：历史量价基础分 + 短期趋势 + 实时主题分 - 风险惩罚。"""
    try:
        from theme_signal import get_theme_signals, get_theme_reason
        theme_signals = get_theme_signals(date_str)
    except Exception as e:
        print(f"[Theme] 加载失败，主题分归零: {e}")
        theme_signals = {"scores": {}, "reasons": {}, "source": "none"}

    ranked = []
    for item in pool:
        code = item["code"]
        name = item["name"]
        df = _get_price_for_decision(code, date_str)
        features = _calc_short_race_features(df)
        if not features:
            continue

        base = score_stock(df) or {}
        historical_score = float(base.get("score", 50.0))
        fresh_map = theme_signals.get("fresh_theme_scores") or theme_signals.get("scores") or {}
        stale_map = theme_signals.get("stale_theme_scores") or {}
        fresh_raw = float(fresh_map.get(code, 0.0))
        stale_raw = float(stale_map.get(code, 0.0))
        fresh_theme_score = float(np.clip(50 + fresh_raw * 50, 0, 100))
        stale_theme_score = float(np.clip(50 + stale_raw * 50, 0, 100))

        from features import _SHORT_RACE_PRICE_CONFIRM_CODES, _apply_price_confirmation_inline
        fresh_theme_score = _apply_price_confirmation_inline(code, fresh_raw, fresh_theme_score, features)

        rotation_penalty = _get_rotation_penalty(code)
        final_score = (
            fresh_theme_score * 0.25 +
            stale_theme_score * 0.10 +
            features["trend_score"] * 0.30 +
            historical_score * 0.20 -
            features.get("risk_penalty", 0.0) * 0.15 -
            rotation_penalty
        )
        final_score = float(np.clip(final_score, 0, 100))

        reason = ""
        try:
            reason = get_theme_reason(code, theme_signals)
        except Exception:
            reason = "主题信号未提供说明。"

        ranked.append({
            "code": code,
            "name": name,
            "category": item.get("category", ""),
            "score": round(float(final_score), 2),
            "historical_score": round(float(historical_score), 2),
            "fresh_theme_score": round(fresh_theme_score, 2),
            "stale_theme_score": round(stale_theme_score, 2),
            "fresh_theme_raw": round(fresh_raw, 2),
            "stale_theme_raw": round(stale_raw, 2),
            # 兼容旧面板/档案字段名（主题以盘后新鲜新闻为准）
            "theme_score": round(fresh_theme_score, 2),
            "theme_raw": round(fresh_raw, 2),
            "theme_reason": reason,
            "pred_return": None,
            **features,
        })

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked, theme_signals


def _inject_llm_views_into_signals(theme_signals, llm_decision):
    """将大模型态度分注入到主题信号中（三条使用路径）。

    路径 1: 覆盖主题分 — LLM 对某 ETF 的态度分直接替换该 ETF 的主题信号
    路径 2: 降低评分闸门 — max|score| >= 0.5 时闸门从 65 降至 55
    路径 3: 仓位比例提示 — 取 min(LLM 建议比例, 规则计算比例)
    """
    if not llm_decision or not isinstance(llm_decision, dict):
        return theme_signals

    views = llm_decision.get("per_etf_view") or llm_decision.get("views") or []
    if not views:
        return theme_signals

    scores = dict(theme_signals.get("scores") or {})
    fresh_scores = dict(theme_signals.get("fresh_theme_scores") or scores)
    reasons = dict(theme_signals.get("reasons") or {})
    llm_hints = dict(theme_signals.get("llm_hints") or {})

    max_abs = 0.0
    for view in views:
        code = str(view.get("code", "")).zfill(6)
        score = float(view.get("score", 0.0) or 0.0)
        reason = view.get("reason", "") or view.get("reason_zh", "")
        if code and score != 0.0:
            scores[code] = score
            fresh_scores[code] = score
            reasons[code] = f"LLM: {reason}" if reason else "LLM 态度覆盖"
            llm_hints[code] = {"score": score, "reason": reason}
            max_abs = max(max_abs, abs(score))

    theme_signals["scores"] = scores
    theme_signals["fresh_theme_scores"] = fresh_scores
    theme_signals["reasons"] = reasons
    theme_signals["llm_hints"] = llm_hints
    theme_signals["llm_max_abs_score"] = max_abs

    # 路径 2: 降低评分闸门
    if max_abs >= 0.5:
        gate_mode = os.environ.get("SCORE_GATE_MODE", "dynamic")
        if gate_mode == "dynamic":
            theme_signals["score_gate_override"] = SCORE_GATE_DYNAMIC_FLOOR

    # 路径 3: 仓位比例提示
    position_hint = llm_decision.get("position_ratio_hint")
    if position_hint is not None:
        theme_signals["position_ratio_hint"] = float(position_hint)

    return theme_signals


def market_avg_score(date_str=None) -> float | None:
    """返回宽基 ETF 的"5日涨+3日涨×0.5"均值，供进攻池开关与仓位评估共用。"""
    refs = ["510300", "159915", "588000"]
    scores = []
    for code in refs:
        df = _get_price_for_decision(code, date_str)
        features = _calc_short_race_features(df)
        if not features:
            continue
        scores.append(features["ret_5d"] + features["ret_3d"] * 0.5)
    if not scores:
        return None
    return float(np.mean(scores))

def rank_stocks(pool):
    """对候选池打分排序（含K线缓存，同日调用秒回）—— 固定权重兜底"""
    results = []
    for item in pool:
        code = item["code"]
        name = item["name"]
        try:
            df = _get_price_for_decision(code)
            factors = score_stock(df)
            if factors:
                results.append({
                    "code": code,
                    "name": name,
                    "source": item.get("category", item.get("source", "")),
                    **factors,
                })
        except Exception as e:
            print(f"[Score] {code}: {e}")
    results.sort(key=lambda x: x["score"], reverse=True)
    return results

