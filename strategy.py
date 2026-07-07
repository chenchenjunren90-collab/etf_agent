"""ETF 日内投资决策主入口（大模型 + 规则引擎双层架构）。

本模块是每日预测的编排中心，负责：
  1. 加载交易池（基础池 + 动态进攻池）
  2. 调用评分模块排名 ETF
  3. 注入 LLM 态度分（若有）
  4. 逐层执行风控规则（仓位限制 / 经济日历硬顶 / 评分闸门）
  5. 调用资金分配模块输出最终持仓

模块拆分说明（从原 strategy.py 拆出）：
  - pool.py       → TRADING_POOL, OFFENSIVE_POOL, Cache
  - indicators.py → RSI, MACD, 动量, 量比, 趋势, 布林带
  - features.py   → _get_price_for_decision, _calc_short_race_features
  - scoring.py    → rank_etfs_short_race, market_avg_score, _inject_llm_views_into_signals
  - position.py   → evaluate_market_regime, allocate_short_race, 仓位风控

本文件保留所有外部模块的 re-export（from strategy import ... 仍可用）。
"""

from __future__ import annotations

import os
import time
from typing import Any

# ================================================
# 从拆分子模块 re-export（保持外部 import 兼容）
# ================================================
from pool import (
    TRADING_POOL,
    OFFENSIVE_POOL,
    OFFENSIVE_ON_THRESHOLD,
    OFFENSIVE_OFF_THRESHOLD,
    Cache,
    _pool_cache,
    _price_cache,
    get_stock_pool,
    get_trading_pool,
)

from features import (
    _get_price_for_decision,
    _load_local_price,
    _calc_short_race_features,
    _score_to_0_100,
    apply_price_confirmation,
)

from scoring import (
    RACE_MAX_POSITIONS,
    RACE_BASE_WEIGHTS,
    RACE_MIN_INVEST_RATIO,
    SCORE_GATE,
    SCORE_GATE_DYNAMIC_FLOOR,
    ECON_TIER1_CAP, ECON_TIER2_CAP, ECON_TIER3_CAP,
    MAX_SINGLE_WEIGHT,
    score_stock,
    rank_stocks,
    rank_etfs_short_race,
    market_avg_score,
    _inject_llm_views_into_signals,
    reset_rotation_tracker,
    _update_rotation_tracker,
)

from position import (
    MIN_AMOUNT,
    evaluate_market_regime,
    short_race_max_positions,
    adjust_invest_ratio_by_news,
    allocate_short_race,
)

# 也兼容旧版引用


# ================================================
# 核心决策函数
# ================================================

def _re_rank_with_signals(pool: list[dict], theme_signals: dict, date_str: str) -> list[dict]:
    """LLM 注入新 theme scores 后重新排名。"""
    import theme_signal as ts_mod

    orig = ts_mod.get_theme_signals

    def _get(_date=None):
        return theme_signals

    ts_mod.get_theme_signals = _get
    try:
        ranked, _ = rank_etfs_short_race(pool, date_str=date_str)
    finally:
        ts_mod.get_theme_signals = orig
    return ranked


def _empty_cash_result(
    date_str: str,
    total_capital: float,
    ranked: list[dict],
    theme_signals: dict,
    market_reason: str,
    *,
    llm_trace: dict | None = None,
) -> dict[str, Any]:
    """统一构造空仓输出。"""
    summary = {
        "total_candidates_scored": len(ranked),
        "stocks_held": 0,
        "capital_used": 0,
        "cash_reserve": int(total_capital),
        "utilization_rate": 0.0,
        "held_stocks": [],
        "invest_ratio": 0.0,
        "mode": "short_race_cash",
    }
    return {
        "date": date_str,
        "allocations": {},
        "summary": summary,
        "ranked": ranked[:10],
        "theme_signals": {
            "source": theme_signals.get("source"),
            "updated_at": theme_signals.get("updated_at"),
            "market_view": theme_signals.get("market_view"),
            "hot_keywords": theme_signals.get("hot_keywords", []),
            "auto_news": theme_signals.get("auto_news", {}),
        },
        "market_reason": market_reason,
        "reasoning": f"Hold cash. Market: {market_reason}",
        "llm_trace": llm_trace,
    }


def build_short_race_reasoning(
    ranked: list[dict],
    result: dict,
    market_reason: str,
    theme_signals: dict,
) -> str:
    """生成决策推理文本（用于审计日志与仪表盘展示）。"""
    held = result.get("summary", {}).get("held_stocks", [])
    if not held:
        return f"Short-race strategy holds cash. Market: {market_reason}"

    parts = [
        "Short-race ETF rotation: price-only historical model plus real-time theme overlay.",
        f"Market regime: {market_reason}.",
        f"Theme source: {theme_signals.get('source', 'unknown')}, view={theme_signals.get('market_view', '')}.",
    ]

    for stock in held:
        parts.append(
            f"{stock['code']}({stock['name']}): score={stock['score']}, "
            f"trend={stock.get('trend_score')}, fresh_theme={stock.get('fresh_theme_score')}, "
            f"stale_theme={stock.get('stale_theme_score')}, "
            f"1d={stock.get('ret_1d', 0):+.2f}%, 3d={stock.get('ret_3d', 0):+.2f}%, "
            f"5d={stock.get('ret_5d', 0):+.2f}%, volume={stock.get('volume_ratio', 1)}x; "
            f"{stock.get('reason', '')}"
        )

    summary = result.get("summary", {})
    parts.append(
        f"Capital used {summary.get('capital_used', 0):,} CNY "
        f"({summary.get('utilization_rate', 0)}%), cash reserve "
        f"{summary.get('cash_reserve', 0):,} CNY."
    )
    return " ".join(parts)


def run_decision(
    date_str: str,
    total_capital: float,
    *,
    llm_decision: dict[str, Any] | None = None,
    econ_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """每日投资决策主函数（大模型融合模式）。

    规则层（量价 + 趋势 + 池切换 + 闸门 + 空仓）为骨架；
    在此之上接入 LLM 决策：
      1) cash_decision == stay_cash 直接空仓；
      2) per_etf_view 覆盖 theme scores；
      3) position_ratio_hint 与规则评估结果取 min；
      4) 经济日历高重要度公布日按分级硬顶限仓（不可被 LLM 抬高）。

    传入 ``llm_decision=None`` 即退化为纯规则路径。
    """
    sep = "=" * 50
    mode_tag = "LLM+规则" if llm_decision else "纯规则"
    print(f"\n{sep}")
    print(f"  [{mode_tag}] Date={date_str} Capital={total_capital:,.0f} CNY")
    print(sep)

    # 动态池：宽基强势时纳入进攻 ETF，弱势时只用稳健池。
    pool = [dict(item) for item in TRADING_POOL]
    avg_score = market_avg_score(date_str)
    if avg_score is not None and avg_score >= OFFENSIVE_ON_THRESHOLD:
        pool.extend([dict(item) for item in OFFENSIVE_POOL])
        offensive_note = (
            f"宽基 5 日均涨 {avg_score:+.2f}% ≥ {OFFENSIVE_ON_THRESHOLD}% → "
            f"启用进攻池 (+{len(OFFENSIVE_POOL)} 只)"
        )
    else:
        offensive_note = (
            f"宽基 5 日均涨 {avg_score:+.2f}% < {OFFENSIVE_ON_THRESHOLD}% → 仅用稳健池"
            if avg_score is not None
            else "市场数据不足，仅用稳健池"
        )
    print(f"[Step 1/4] Ranking {len(pool)} ETFs ({offensive_note}) ...")
    ranked, theme_signals = rank_etfs_short_race(pool, date_str=date_str)
    print(f"  Scored: {len(ranked)}")

    if ranked:
        top3 = " / ".join(
            f"{s['code']}({s['name']}) score={s['score']}"
            for s in ranked[:3]
        )
        print(f"  TOP3: {top3}")

    # 准备 llm_trace 用于审计输出
    llm_trace = None
    if llm_decision:
        llm_trace = {
            "regime": llm_decision.get("regime"),
            "regime_reason": llm_decision.get("regime_reason"),
            "cash_decision": llm_decision.get("cash_decision"),
            "position_ratio_hint": llm_decision.get("position_ratio_hint"),
            "summary_zh": llm_decision.get("summary_zh"),
            "per_etf_view": llm_decision.get("per_etf_view", []),
            "econ_drivers": llm_decision.get("econ_drivers", []),
            "news_drivers": llm_decision.get("news_drivers", []),
            "hard_rules_applied": [],
        }

    # LLM cash_decision == stay_cash 直接空仓
    if llm_decision and llm_decision.get("cash_decision") == "stay_cash":
        reason = (
            "LLM 判定 stay_cash："
            f"{llm_decision.get('summary_zh') or llm_decision.get('regime_reason') or ''}"
        )
        if llm_trace:
            llm_trace["hard_rules_applied"].append("llm_stay_cash")
        print(f"  LLM stay_cash → 空仓。{reason}")
        return _empty_cash_result(date_str, total_capital, ranked, theme_signals, reason,
                                  llm_trace=llm_trace)

    # LLM per_etf_view 覆盖 theme scores 并重新排序
    if llm_decision and llm_decision.get("per_etf_view"):
        theme_signals = _inject_llm_views_into_signals(theme_signals, llm_decision)
        ranked = _re_rank_with_signals(pool, theme_signals, date_str)
        if ranked:
            top3 = " / ".join(
                f"{s['code']}({s['name']}) score={s['score']}"
                for s in ranked[:3]
            )
            print(f"  TOP3 (LLM-rescored): {top3}")

    print("[Step 2/4] Evaluating market regime...")
    invest_ratio, market_reason = evaluate_market_regime(date_str)
    invest_ratio, market_reason = adjust_invest_ratio_by_news(
        invest_ratio, market_reason, theme_signals
    )

    # 与 LLM 的 position_ratio_hint 取 min（只往低调，不抬高）
    if llm_decision and "position_ratio_hint" in llm_decision:
        hint = float(llm_decision.get("position_ratio_hint") or 0.0)
        hint = max(0.0, min(1.0, hint))
        if hint < invest_ratio:
            old_ratio = invest_ratio
            invest_ratio = hint
            market_reason = f"{market_reason}；LLM 建议 {hint:.0%} (规则 {old_ratio:.0%})，取较低值"
            if llm_trace:
                llm_trace["hard_rules_applied"].append("llm_lower_ratio")

    force_cap = os.environ.get("FORCE_POSITION_CAP", "").strip()
    if force_cap:
        try:
            cap = float(force_cap)
            if 0.0 < cap < 1.0 and invest_ratio > cap:
                old_ratio = invest_ratio
                invest_ratio = cap
                market_reason = f"{market_reason}；数据质量降级仓位上限 {cap:.0%} (原{old_ratio:.0%})"
                if llm_trace:
                    llm_trace["hard_rules_applied"].append("force_position_cap")
        except ValueError:
            pass

    # 经济日历分级仓位上限（根据高影响事件数量动态调整）
    if econ_payload and econ_payload.get("has_high_impact_event"):
        high_count = econ_payload.get("high_impact_count", 1)
        if high_count <= 2:
            econ_cap = ECON_TIER1_CAP  # 1-2条: 85%
        elif high_count <= 5:
            econ_cap = ECON_TIER2_CAP  # 3-5条: 75%
        else:
            econ_cap = ECON_TIER3_CAP  # 6+条: 65%
        if invest_ratio > econ_cap:
            old_ratio = invest_ratio
            invest_ratio = econ_cap
            market_reason = (
                f"{market_reason}；经济日历{high_count}条高影响事件 → "
                f"分级上限{econ_cap:.0%} (原{old_ratio:.0%})"
            )
            if llm_trace:
                llm_trace["hard_rules_applied"].append(f"econ_tier_cap_{econ_cap:.0%}")

    # 评分闸门
    top_score = float(ranked[0]["score"]) if ranked else 0.0
    override = theme_signals.get("score_gate_override")
    effective_gate = float(override) if override is not None else SCORE_GATE
    gate_note = ""
    if os.environ.get("SCORE_GATE_MODE", "").strip().lower() == "dynamic" and llm_decision:
        per_view = llm_decision.get("per_etf_view") or []
        if per_view:
            max_abs = max((abs(float(e.get("score") or 0.0)) for e in per_view), default=0.0)
            if max_abs >= 0.5:
                effective_gate = SCORE_GATE_DYNAMIC_FLOOR
                gate_note = f"（LLM 强信号 max|score|={max_abs:.2f} → 闸门 {SCORE_GATE}→{effective_gate}）"
                if llm_trace:
                    llm_trace["hard_rules_applied"].append("score_gate_lowered_by_llm")
    if invest_ratio > 0 and top_score < effective_gate:
        invest_ratio = 0.0
        market_reason = f"{market_reason}；最高分 {top_score:.1f} < {effective_gate} 闸门，强制空仓{gate_note}"
        if llm_trace:
            llm_trace["hard_rules_applied"].append("score_gate")
    print(f"  Invest ratio: {invest_ratio:.0%} ({market_reason})")

    print("[Step 3/4] Allocating concentrated race portfolio...")
    dyn_max = short_race_max_positions(theme_signals)
    result = allocate_short_race(ranked, total_capital, invest_ratio, max_positions=dyn_max)
    result["date"] = date_str
    result["ranked"] = ranked[:10]
    result["theme_signals"] = {
        "source": theme_signals.get("source"),
        "updated_at": theme_signals.get("updated_at"),
        "market_view": theme_signals.get("market_view"),
        "hot_keywords": theme_signals.get("hot_keywords", []),
        "auto_news": theme_signals.get("auto_news", {}),
    }
    result["market_reason"] = market_reason
    result["reasoning"] = build_short_race_reasoning(ranked, result, market_reason, theme_signals)
    # 更新轮动追踪
    top_codes = [item["code"] for item in (result.get("summary", {}).get("held_stocks", []) or [])]
    _update_rotation_tracker(top_codes)

    if llm_trace:
        llm_trace["final_invest_ratio"] = round(float(invest_ratio), 3)
        llm_trace["final_top_score"] = round(top_score, 2)
        try:
            from scoring import _rotation_tracker
            llm_trace["rotation_tracker"] = dict(_rotation_tracker)
        except (ImportError, NameError):
            llm_trace["rotation_tracker"] = {}
    result["llm_trace"] = llm_trace

    summary = result.get("summary", {})
    print("[Step 4/4] Done.")
    print(f"  Mode: {summary.get('mode', '?')}")
    print(f"  Positions: {summary.get('stocks_held', 0)}")
    print(f"  Used: {summary.get('capital_used', 0):,} CNY ({summary.get('utilization_rate', 0)}%)")
    print(f"  Cash: {summary.get('cash_reserve', 0):,} CNY")
    print(sep + "\n")

    return result


# ================================================
# 管理员接口
# ================================================

if __name__ == '__main__':
    from datetime import datetime
    print("=== FIRST CALL ===")
    t0 = time.time()
    result = run_decision("2026-04-27", 200000)
    t1 = time.time()
    print(f"\nTime: {t1-t0:.1f}s")
    print("Allocations:", {k: v for k, v in result.get("allocations", {}).items()})
