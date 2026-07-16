"""ETF 日内投资决策主入口（大模型 + 规则引擎双层架构）。

本模块是每日预测的编排中心，负责：
  1. 加载交易池（基础池 + 动态进攻池）
  2. 调用评分模块排名 ETF
  3. 记录 LLM 态度（默认仅审计，不控制交易评分）
  4. 用严格历史样本验证成本后盈利证据，不足则空仓
  5. 逐层执行风控规则并输出最终持仓

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
    event_supported_offensive_pool,
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
    apply_stability_overlay,
    allocate_short_race,
)
from decision_integrity import apply_concentration_risk
from goal_state import apply_goal_overlay
from profitability_evidence import evaluate_trade_candidates


def _resolve_score_gate(rule_gate: float, evidence_floor: Any) -> float:
    """Keep downstream score gating compatible with an audited probe floor."""
    if evidence_floor is None:
        return float(rule_gate)
    return min(float(rule_gate), float(evidence_floor))
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
    profitability_gate: dict | None = None,
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
    result = {
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
    if profitability_gate is not None:
        result["profitability_gate"] = profitability_gate
    return result


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


def _drop_stale_bar_names(
    ranked: list[dict[str, Any]],
    integrity_ctx: dict[str, Any] | None,
    *,
    llm_trace: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """从排名中剔除 K 线未跟上的标的（尤其进攻池局部陈旧时整体 stale_ratio 仍可能 <50%）。"""
    per_code = ((integrity_ctx or {}).get("price_audit") or {}).get("per_code") or {}
    if not per_code or not ranked:
        return ranked
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for item in ranked:
        code = str(item.get("code") or "").zfill(6)
        info = per_code.get(code)
        if info is not None and not info.get("ok", True):
            dropped.append(code)
            continue
        kept.append(item)
    if dropped:
        msg = f"dropped_stale_bars:{','.join(dropped)}"
        print(f"  剔除陈旧K线标的: {', '.join(dropped)}")
        if llm_trace is not None:
            llm_trace.setdefault("hard_rules_applied", []).append(msg)
    return kept


def run_decision(
    date_str: str,
    total_capital: float,
    *,
    llm_decision: dict[str, Any] | None = None,
    econ_payload: dict[str, Any] | None = None,
    recent_risk: dict[str, Any] | None = None,
    integrity_ctx: dict[str, Any] | None = None,
    goal_state: dict[str, Any] | None = None,
    theme_signals_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """每日投资决策主函数（大模型融合模式）。

    因素主次（高→低，次级不得压过清晰主信号）：
      1) 数据完整性：行情陈旧则禁用 LLM 重排/仓位提示；
      2) 盈利证据：严格历史相似状态的成本后优势，无优势即空仓；
      3) 主信号：综合分只负责提出候选，不再拥有最终入场权；
      4) 风险预算：稳健层/经济日历/评分闸门（管仓位大小）；
      5) 连持倾向：连续持有同名仅软性降分，不禁买、不强制分散，
         明显领先时不因连持翻盘；
      6) LLM：仅行情新鲜时可用，只可解释和审计，不可突破硬顶。

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
    event_offensive = event_supported_offensive_pool(theme_signals_override)
    if avg_score is not None and avg_score >= OFFENSIVE_ON_THRESHOLD:
        pool.extend([dict(item) for item in OFFENSIVE_POOL])
        offensive_note = (
            f"宽基复合趋势 {avg_score:+.2f}% ≥ {OFFENSIVE_ON_THRESHOLD}% → "
            f"启用进攻池 (+{len(OFFENSIVE_POOL)} 只)"
        )
    elif event_offensive:
        pool.extend(event_offensive)
        offensive_note = (
            f"宽基趋势未达进攻阈值，但新鲜新闻直接映射 → "
            f"仅评估事件进攻池 (+{len(event_offensive)} 只)"
        )
    else:
        offensive_note = (
            f"宽基复合趋势 {avg_score:+.2f}% < {OFFENSIVE_ON_THRESHOLD}% → 仅用稳健池"
            if avg_score is not None
            else "市场数据不足，仅用稳健池"
        )
    print(f"[Step 1/4] Ranking {len(pool)} ETFs ({offensive_note}) ...")
    if theme_signals_override is not None:
        theme_signals = dict(theme_signals_override)
        ranked = _re_rank_with_signals(pool, theme_signals, date_str)
    else:
        ranked, theme_signals = rank_etfs_short_race(pool, date_str=date_str)
    print(f"  Scored: {len(ranked)}")

    # 行情陈旧时禁用 LLM 重排/仓位提示（避免用过时K线+幻觉叙事把排序钉死），
    # 但不禁止同一只 ETF 连续入选——市场真排第一就该选它。
    block_llm = bool(integrity_ctx and integrity_ctx.get("block_llm_rescore"))

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

    ranked = _drop_stale_bar_names(ranked, integrity_ctx, llm_trace=llm_trace)

    # LLM per_etf_view 覆盖 theme scores 并重新排序（行情陈旧时跳过）
    if block_llm and llm_trace:
        llm_trace["hard_rules_applied"].append("llm_rescore_blocked_stale_prices")
    if llm_decision and llm_decision.get("per_etf_view") and not block_llm:
        theme_signals = _inject_llm_views_into_signals(theme_signals, llm_decision)
        ranked = _re_rank_with_signals(pool, theme_signals, date_str)
        if ranked:
            top3 = " / ".join(
                f"{s['code']}({s['name']}) score={s['score']}"
                for s in ranked[:3]
            )
            print(f"  TOP3 (LLM-rescored): {top3}")
        ranked = _drop_stale_bar_names(ranked, integrity_ctx, llm_trace=llm_trace)
    # stay_cash 放在重打分之后；行情陈旧时忽略 LLM 空仓建议（与 rescore 同口径）
    if (
        llm_decision
        and llm_decision.get("cash_decision") == "stay_cash"
        and not block_llm
    ):
        top0 = float(ranked[0]["score"]) if ranked else 0.0
        reason = (
            "LLM 建议 stay_cash："
            f"{llm_decision.get('summary_zh') or llm_decision.get('regime_reason') or ''}"
        )
        if top0 < SCORE_GATE:
            if llm_trace:
                llm_trace["hard_rules_applied"].append("llm_stay_cash")
            print(f"  LLM stay_cash + 最高分{top0:.1f}<{SCORE_GATE} → 空仓。{reason}")
            return _empty_cash_result(date_str, total_capital, ranked, theme_signals, reason,
                                      llm_trace=llm_trace)
        if llm_trace:
            llm_trace["hard_rules_applied"].append("llm_stay_cash_ignored_score_above_gate")
        print(
            f"  LLM stay_cash 忽略（最高分{top0:.1f}>={SCORE_GATE}，由规则继续决策）。{reason}"
        )
    elif block_llm and llm_decision and llm_decision.get("cash_decision") == "stay_cash" and llm_trace:
        llm_trace["hard_rules_applied"].append("llm_stay_cash_ignored_stale_prices")

    # 独立盈利证据层拥有最终入场否决权。综合分只提出候选；相似历史
    # 状态没有显示成本后正优势、新闻缺乏直接事件映射，或入场已过度延伸，
    # 都必须拒绝，而不是靠降低仓位掩盖错误信号。
    proposed_ranked = list(ranked)
    ranked, profitability_gate = evaluate_trade_candidates(
        ranked,
        theme_signals,
        date_str,
        recent_submit_history=(integrity_ctx or {}).get("recent_submit_history") or [],
    )
    if llm_trace is not None:
        llm_trace["llm_score_control_enabled"] = bool(
            theme_signals.get("llm_score_control_enabled")
        )
        llm_trace["hard_rules_applied"].append("profitability_evidence_gate")
    if not ranked:
        reason = "盈利证据不足：历史相似状态未显示可靠的成本后正收益，今日保持空仓。"
        if llm_trace is not None:
            llm_trace["summary_zh_original"] = llm_trace.get("summary_zh")
            llm_trace["summary_zh"] = reason
        print(f"  [EvidenceGate] {reason}")
        return _empty_cash_result(
            date_str,
            total_capital,
            proposed_ranked,
            theme_signals,
            reason,
            llm_trace=llm_trace,
            profitability_gate=profitability_gate,
        )
    evidence_cap = float(profitability_gate.get("exposure_cap") or 0.0)
    evidence_max_positions = int(profitability_gate.get("max_positions") or 1)
    print(
        "  [EvidenceGate] "
        f"mode={profitability_gate.get('mode')} selected={profitability_gate.get('selected_code')} "
        f"cap={evidence_cap:.0%}"
    )

    print("[Step 2/4] Evaluating market regime...")
    invest_ratio, market_reason = evaluate_market_regime(date_str)
    invest_ratio, market_reason = adjust_invest_ratio_by_news(
        invest_ratio, market_reason, theme_signals
    )
    if evidence_cap > 0 and invest_ratio > evidence_cap:
        old_ratio = invest_ratio
        invest_ratio = evidence_cap
        market_reason = (
            f"{market_reason}；盈利证据层仓位上限 {evidence_cap:.0%} "
            f"(原{old_ratio:.0%})"
        )

    # LLM position_ratio_hint 仅审计：仓位由 regime + 新闻 + 经济日历 + stable
    # 决定。校正后全链路回测显示 min(规则, hint) 系统性压仓，抹掉规则 alpha。
    if llm_decision and "position_ratio_hint" in llm_decision:
        hint = float(llm_decision.get("position_ratio_hint") or 0.0)
        hint = max(0.0, min(1.0, hint))
        if llm_trace:
            llm_trace["position_ratio_hint_audit"] = hint
            if block_llm:
                llm_trace["hard_rules_applied"].append("llm_ratio_hint_ignored_stale_prices")
            else:
                llm_trace["hard_rules_applied"].append("llm_ratio_hint_ignored_rules_own_sizing")

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

    # 评分闸门（默认 static；仅 SCORE_GATE_MODE=dynamic 时允许降闸）
    top_score = float(ranked[0]["score"]) if ranked else 0.0
    gate_mode = os.environ.get("SCORE_GATE_MODE", "static").strip().lower()
    override = theme_signals.get("score_gate_override")
    effective_gate = float(SCORE_GATE)
    gate_note = ""
    if (
        not block_llm
        and gate_mode == "dynamic"
        and override is not None
    ):
        effective_gate = float(override)
        if effective_gate < SCORE_GATE:
            gate_note = f"（LLM 动态闸门 {SCORE_GATE}→{effective_gate}）"
            if llm_trace:
                llm_trace["hard_rules_applied"].append("score_gate_lowered_by_llm")
    elif (
        not block_llm
        and gate_mode == "dynamic"
        and llm_decision
        and override is None
    ):
        per_view = llm_decision.get("per_etf_view") or []
        if per_view:
            max_abs = max((abs(float(e.get("score") or 0.0)) for e in per_view), default=0.0)
            if max_abs >= 0.5:
                effective_gate = SCORE_GATE_DYNAMIC_FLOOR
                gate_note = f"（LLM 强信号 max|score|={max_abs:.2f} → 闸门 {SCORE_GATE}→{effective_gate}）"
                if llm_trace:
                    llm_trace["hard_rules_applied"].append("score_gate_lowered_by_llm")
    elif block_llm and llm_trace and gate_mode == "dynamic":
        llm_trace["hard_rules_applied"].append("score_gate_dynamic_blocked_stale_prices")
    evidence_score_floor = profitability_gate.get("score_gate_floor")
    if evidence_score_floor is not None:
        effective_gate = _resolve_score_gate(effective_gate, evidence_score_floor)
        gate_note = (
            f"（盈利证据试探门槛 {SCORE_GATE}→{effective_gate}；"
            "仓位仍受证据层上限约束）"
        )
        if llm_trace:
            llm_trace["hard_rules_applied"].append("profitability_evidence_score_gate")
    if invest_ratio > 0 and top_score < effective_gate:
        invest_ratio = 0.0
        market_reason = f"{market_reason}；最高分 {top_score:.1f} < {effective_gate} 闸门，强制空仓{gate_note}"
        if llm_trace:
            llm_trace["hard_rules_applied"].append("score_gate")

    stability_audit = None
    stability_max_positions = None
    if invest_ratio > 0:
        invest_ratio, market_reason, stability_max_positions, stability_audit = apply_stability_overlay(
            invest_ratio,
            market_reason,
            ranked,
            theme_signals,
            recent_risk=recent_risk,
        )
        if llm_trace and stability_audit and stability_audit["final_invest_ratio"] < stability_audit["original_invest_ratio"]:
            llm_trace["hard_rules_applied"].append("stability_overlay")
    print(f"  Invest ratio: {invest_ratio:.0%} ({market_reason})")

    print("[Step 3/4] Allocating concentrated race portfolio...")
    dyn_max = min(short_race_max_positions(theme_signals), evidence_max_positions)
    if stability_max_positions is not None:
        # 下游风控只能收紧上游盈利证据许可，绝不能把 evidence_max_positions=1
        # 重新扩大成 2；否则第二只未通过独立校准的 ETF 也会被提交。
        dyn_max = min(dyn_max, int(stability_max_positions))

    concentration_audit: dict[str, Any] | None = None
    if integrity_ctx:
        ranked, invest_ratio, dyn_max, concentration_audit = apply_concentration_risk(
            ranked, invest_ratio, dyn_max, integrity_ctx
        )
        if concentration_audit.get("applied"):
            market_reason = (
                f"{market_reason}；连持倾向"
                f"（{'；'.join(concentration_audit.get('notes') or [])}）"
            )
            print(f"  [RepeatTilt] {'；'.join(concentration_audit.get('notes') or [])}")
            if llm_trace:
                llm_trace["hard_rules_applied"].append("repeat_holding_tilt")
            if ranked:
                top3 = " / ".join(
                    f"{s['code']}({s['name']}) score={s['score']}"
                    for s in ranked[:3]
                )
                print(f"  TOP3 (after tilt): {top3}")
            # 倾斜后重检闸门，避免次级因素把第一名换掉后仍按旧分入场/漏检空仓。
            top_score = float(ranked[0]["score"]) if ranked else 0.0
            if invest_ratio > 0 and top_score < effective_gate:
                invest_ratio = 0.0
                market_reason = (
                    f"{market_reason}；倾斜后最高分 {top_score:.1f} < {effective_gate} 闸门，强制空仓"
                )
                if llm_trace:
                    llm_trace["hard_rules_applied"].append("score_gate_after_tilt")

    goal_audit: dict[str, Any] | None = None
    invest_ratio, dyn_max, goal_audit = apply_goal_overlay(
        invest_ratio, dyn_max, ranked, goal_state
    )
    if goal_audit:
        notes = goal_audit.get("notes") or []
        if notes:
            market_reason = f"{market_reason}; goal control: {'; '.join(notes)}"
        if (
            llm_trace
            and goal_audit["final_invest_ratio"]
            < goal_audit["original_invest_ratio"]
        ):
            llm_trace["hard_rules_applied"].append("goal_control_overlay")

    result = allocate_short_race(
        ranked,
        total_capital,
        invest_ratio,
        max_positions=dyn_max,
        max_single_weight=(
            float(stability_audit["max_single_weight"])
            if stability_audit and stability_audit.get("max_single_weight") is not None
            else None
        ),
    )
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
    result["profitability_gate"] = profitability_gate
    execution_dropped = result.get("summary", {}).get("execution_dropped") or []
    if execution_dropped and llm_trace is not None:
        llm_trace["summary_zh_original"] = llm_trace.get("summary_zh")
        submitted_names = [
            f"{item.get('code')}({item.get('name')})"
            for item in result.get("summary", {}).get("held_stocks", [])
        ]
        llm_trace["summary_zh"] = (
            "经交易手数校验，最终可提交持仓为："
            + ("、".join(submitted_names) if submitted_names else "空仓")
            + "。原始模型观点保留在审计字段中。"
        )
        llm_trace.setdefault("hard_rules_applied", []).append("execution_lot_filter")
    result["stability_overlay"] = stability_audit
    if goal_audit:
        result["goal_overlay"] = goal_audit
    if concentration_audit:
        result["concentration_risk"] = concentration_audit
    if integrity_ctx:
        result["integrity_context"] = {
            "price_stale": integrity_ctx.get("price_stale"),
            "block_llm_rescore": block_llm,
            "expected_bar_date": (integrity_ctx.get("price_audit") or {}).get("expected_bar_date"),
            "stale_ratio": (integrity_ctx.get("price_audit") or {}).get("stale_ratio"),
            "sole_symbol_streak": integrity_ctx.get("sole_symbol_streak"),
            "holding_streaks": integrity_ctx.get("holding_streaks"),
        }
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
