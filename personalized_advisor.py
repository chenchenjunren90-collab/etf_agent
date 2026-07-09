"""Scale / reselect personal advice from daily ranked candidates.

IMPORTANT: This module is READ-ONLY against competition artifacts
(data/daily_output/*, data/agent_kb/*). It never writes submit.json / full.json
or rebuilds the official knowledge base. See competition_guard.py.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_kb import load_knowledge_base
from daily_pnl import _load_bar
from info_collector import DISCLAIMER, FOCUS_LABELS, RISK_LABELS
from pool import OFFENSIVE_POOL, TRADING_POOL


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
COMPETITION_CAPITAL = 500_000.0
MIN_AMOUNT = 5000.0
LOT = 100

# Focus → category / code boosts
FOCUS_BOOST: dict[str, dict[str, float]] = {
    "dividend": {
        "categories": {
            "高股息防御": 22, "大盘蓝筹": 10, "全市场": 4,
            "创业板": -12, "科创板": -12, "创蓝筹": -10, "券商周期": -6, "医疗": -4,
        },
        "codes": {"510880": 28, "510050": 12},
    },
    "broad": {
        "categories": {
            "全市场": 20, "大盘蓝筹": 14, "中盘成长": 10,
            "券商周期": -8, "医疗": -8, "创业板": -4, "科创板": -4,
        },
        "codes": {"510300": 22, "510050": 14, "510500": 12, "510330": 12, "159338": 12},
    },
    "growth": {
        "categories": {
            "创业板": 22, "科创板": 22, "创蓝筹": 18, "中盘成长": 12,
            "高股息防御": -10, "大盘蓝筹": -4, "商品避险": -8,
        },
        "codes": {"159915": 24, "588000": 24, "159949": 18, "510500": 12},
    },
    "sector": {
        "categories": {
            "券商周期": 20, "医疗": 20,
            "全市场": -6, "高股息防御": -6, "大盘蓝筹": -4,
        },
        "codes": {"512880": 22, "512010": 22},
    },
    "auto": {"categories": {}, "codes": {}},
}

# Risk style → soft category tilt (on top of focus)
RISK_TILT: dict[str, dict[str, float]] = {
    "conservative": {
        "高股息防御": 12,
        "大盘蓝筹": 8,
        "全市场": 4,
        "商品避险": 6,
        "创业板": -10,
        "科创板": -10,
        "创蓝筹": -8,
        "券商周期": -4,
    },
    "balanced": {},
    "aggressive": {
        "创业板": 10,
        "科创板": 10,
        "创蓝筹": 8,
        "券商周期": 6,
        "医疗": 4,
        "高股息防御": -4,
    },
}

RISK_PARAMS: dict[str, dict[str, Any]] = {
    "conservative": {
        "invest_cap": 0.35,
        "max_positions": 2,
        "max_single": 0.22,
        "min_score": 48.0,
        "weights": [0.60, 0.40],
    },
    "balanced": {
        "invest_cap": 0.60,
        "max_positions": 3,
        "max_single": 0.28,
        "min_score": 46.0,
        "weights": [0.45, 0.30, 0.25],
    },
    "aggressive": {
        "invest_cap": 0.85,
        "max_positions": 3,
        "max_single": 0.35,
        "min_score": 44.0,
        "weights": [0.50, 0.30, 0.20],
    },
}

POOL_META = {
    str(x["code"]).zfill(6): x for x in (TRADING_POOL + OFFENSIVE_POOL)
}


def _load_full(date_str: str) -> dict[str, Any] | None:
    path = OUTPUT_DIR / f"{date_str}_full.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _load_submit(date_str: str) -> list[dict[str, Any]]:
    path = OUTPUT_DIR / f"{date_str}_submit.json"
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    kb = load_knowledge_base(date_str)
    if kb:
        return list(kb.get("competition_output") or [])
    return []


def _price_for(code: str, date_str: str, ranked_item: dict[str, Any] | None = None) -> float | None:
    if ranked_item and ranked_item.get("latest_price"):
        try:
            return float(ranked_item["latest_price"])
        except Exception:
            pass
    bar = _load_bar(code, date_str)
    if bar and bar.get("close"):
        return float(bar["close"])
    return None


def _resolve_date_and_sources(
    date_str: str | None,
    allow_latest_fallback: bool,
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]], bool]:
    requested = (date_str or datetime.now().strftime("%Y-%m-%d"))[:10]
    full = _load_full(requested)
    kb = load_knowledge_base(requested)
    used_fallback = False

    if full is None and kb is None and allow_latest_fallback:
        # Prefer newest full.json
        files = sorted(OUTPUT_DIR.glob("*_full.json"))
        if files:
            requested = files[-1].name.split("_")[0]
            full = _load_full(requested)
            kb = load_knowledge_base(requested)
            used_fallback = True
        else:
            kb = load_knowledge_base(None)
            if kb:
                requested = str(kb.get("date") or requested)
                used_fallback = True

    ranked: list[dict[str, Any]] = []
    if full:
        ranked = list((full.get("strategy_result") or {}).get("ranked") or [])
    return requested, kb, ranked, used_fallback


def _base_invest_ratio(full: dict[str, Any] | None, kb: dict[str, Any] | None) -> float:
    if full:
        summary = (full.get("strategy_result") or {}).get("summary") or {}
        try:
            r = float(summary.get("invest_ratio") or 0)
            if r > 0:
                return r
        except Exception:
            pass
    # fallback from competition utilization
    if kb and not kb.get("is_empty_position"):
        return 0.55
    return 0.0


def _score_with_profile(
    item: dict[str, Any],
    *,
    risk: str,
    focus: str,
    prefer_codes: list[str],
    avoid_codes: list[str],
) -> float:
    code = str(item.get("code") or "").zfill(6)
    if code in avoid_codes:
        return -1e9

    base = float(item.get("score") or 0.0)
    cat = str(item.get("category") or POOL_META.get(code, {}).get("category") or "")

    adj = 0.0
    # risk tilt
    adj += float(RISK_TILT.get(risk, {}).get(cat, 0.0))
    # focus boost
    fb = FOCUS_BOOST.get(focus) or FOCUS_BOOST["auto"]
    adj += float((fb.get("categories") or {}).get(cat, 0.0))
    adj += float((fb.get("codes") or {}).get(code, 0.0))
    # explicit prefer
    if code in prefer_codes:
        adj += 25.0

    return base + adj


def _select_and_allocate(
    ranked: list[dict[str, Any]],
    *,
    capital: float,
    risk: str,
    focus: str,
    prefer_codes: list[str],
    avoid_codes: list[str],
    base_invest_ratio: float,
    date_str: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    params = RISK_PARAMS.get(risk) or RISK_PARAMS["balanced"]
    base = float(base_invest_ratio or 0.0)
    cap = float(params["invest_cap"])

    if base <= 0:
        # Strategy empty → small probe only for non-conservative
        invest_ratio = 0.0 if risk == "conservative" else min(0.20, cap)
    elif risk == "conservative":
        invest_ratio = min(cap, base * 0.70)
    elif risk == "aggressive":
        # Stretch toward the risk cap while respecting signal strength
        invest_ratio = min(cap, max(base * 1.35, (base + cap) * 0.5))
    else:
        invest_ratio = min(cap, base)

    scored = []
    for item in ranked:
        code = str(item.get("code") or "").zfill(6)
        adj = _score_with_profile(
            item,
            risk=risk,
            focus=focus,
            prefer_codes=prefer_codes,
            avoid_codes=avoid_codes,
        )
        if adj <= -1e8:
            continue
        scored.append({**item, "code": code, "adj_score": adj})

    scored.sort(key=lambda x: float(x["adj_score"]), reverse=True)

    min_score = float(params["min_score"])
    eligible = [x for x in scored if float(x.get("score") or 0) >= min_score or x["code"] in prefer_codes]
    if not eligible:
        eligible = scored[:1] if scored else []

    max_pos = int(params["max_positions"])
    # Prefer codes: ensure they enter if present
    selected: list[dict[str, Any]] = []
    for code in prefer_codes:
        for x in eligible:
            if x["code"] == code and x not in selected:
                selected.append(x)
                break
    for x in eligible:
        if len(selected) >= max_pos:
            break
        if x not in selected:
            selected.append(x)

    selected = selected[:max_pos]
    meta = {
        "invest_ratio": round(invest_ratio, 4),
        "max_positions": max_pos,
        "max_single": params["max_single"],
        "candidates_considered": len(scored),
        "top_adj": [
            {
                "code": x["code"],
                "name": x.get("name"),
                "score": round(float(x.get("score") or 0), 2),
                "adj_score": round(float(x["adj_score"]), 2),
            }
            for x in scored[:5]
        ],
    }

    if not selected or invest_ratio <= 0:
        return [], meta

    weights = list(params["weights"])[: len(selected)]
    # pad / trim
    while len(weights) < len(selected):
        weights.append(weights[-1] if weights else 0.3)
    weights = weights[: len(selected)]
    s = sum(weights) or 1.0
    weights = [w / s for w in weights]

    max_single = float(params["max_single"])
    investable = capital * invest_ratio
    holdings: list[dict[str, Any]] = []

    for item, w in zip(selected, weights):
        code = item["code"]
        name = item.get("name") or POOL_META.get(code, {}).get("name") or code
        px = _price_for(code, date_str, item)
        if not px or px <= 0:
            continue
        amount = investable * w
        amount = min(amount, capital * max_single)
        if amount < MIN_AMOUNT:
            continue
        vol = int(amount // px // LOT * LOT)
        if vol <= 0:
            continue
        actual_amount = round(vol * px, 2)
        reason = str(item.get("theme_reason") or "").replace("LLM: ", "").strip()
        if not reason:
            reason = f"综合评分 {float(item.get('score') or 0):.1f}，按您的{RISK_LABELS.get(risk, '')}/{FOCUS_LABELS.get(focus, '')}偏好入选。"
        else:
            reason = reason[:140]

        holdings.append({
            "symbol": code,
            "symbol_name": name,
            "volume": vol,
            "approx_price": round(px, 4),
            "approx_amount": actual_amount,
            "weight_pct": round(actual_amount / capital * 100, 1),
            "score": round(float(item.get("score") or 0), 2),
            "adj_score": round(float(item["adj_score"]), 2),
            "category": item.get("category") or POOL_META.get(code, {}).get("category"),
            "reason": reason,
            "related_news": [],
        })

    return holdings, meta


def _fallback_scale_competition(
    base_holdings: list[dict[str, Any]],
    capital: float,
    date_str: str,
    risk: str,
) -> list[dict[str, Any]]:
    """Last resort: scale competition output, then apply risk invest cap."""
    params = RISK_PARAMS.get(risk) or RISK_PARAMS["balanced"]
    ratio = float(capital) / COMPETITION_CAPITAL
    # also shrink by risk invest_cap vs typical 0.6 competition usage
    risk_scale = float(params["invest_cap"]) / 0.60
    ratio *= min(risk_scale, 1.2)

    out = []
    for h in base_holdings:
        code = str(h.get("symbol") or "").zfill(6)
        name = h.get("symbol_name") or code
        base_vol = int(h.get("volume") or 0)
        if base_vol <= 0:
            continue
        vol = int(base_vol * ratio // LOT * LOT)
        if vol <= 0:
            continue
        item: dict[str, Any] = {"symbol": code, "symbol_name": name, "volume": vol}
        px = _price_for(code, date_str)
        if px and px > 0:
            amount = round(vol * px, 2)
            item["approx_price"] = round(px, 4)
            item["approx_amount"] = amount
            item["weight_pct"] = round(amount / capital * 100, 1)
        out.append(item)
        if len(out) >= int(params["max_positions"]):
            break
    return out


def risk_position_note(risk: str, holdings: list[dict[str, Any]], capital: float, focus: str) -> str:
    invested = sum(float(h.get("approx_amount") or 0) for h in holdings)
    pct = (invested / capital * 100) if capital else 0
    label = RISK_LABELS.get(risk, "均衡")
    flabel = FOCUS_LABELS.get(focus, "跟随策略")
    names = "、".join(h.get("symbol_name") or h.get("symbol") for h in holdings) or "空仓"
    return (
        f"按您的 **{label}** 风格 + **{flabel}** 方向，"
        f"建议配置：{names}（估算仓位约 {pct:.0f}%）。"
    )


def build_personal_advice(
    *,
    capital: float,
    risk_preference: str = "balanced",
    focus: str = "auto",
    prefer_codes: list[str] | None = None,
    avoid_codes: list[str] | None = None,
    date_str: str | None = None,
    allow_latest_fallback: bool = True,
) -> dict[str, Any]:
    """
    Re-select and size ETF holdings from daily ranked candidates
    according to the user's capital / risk / focus.
    """
    risk = risk_preference if risk_preference in RISK_PARAMS else "balanced"
    focus = focus if focus in FOCUS_BOOST else "auto"
    prefer_codes = [str(c).zfill(6) for c in (prefer_codes or [])]
    avoid_codes = [str(c).zfill(6) for c in (avoid_codes or [])]

    requested, kb, ranked, used_fallback = _resolve_date_and_sources(
        date_str, allow_latest_fallback
    )
    full = _load_full(requested)

    if not ranked and not kb and not full:
        return {
            "ok": False,
            "need_run": True,
            "date": requested,
            "error": "暂无可用预测。请先说「测一下今天」生成，或稍后再试。",
        }

    base_ratio = _base_invest_ratio(full, kb)
    holdings: list[dict[str, Any]] = []
    alloc_meta: dict[str, Any] = {}

    if ranked:
        holdings, alloc_meta = _select_and_allocate(
            ranked,
            capital=float(capital),
            risk=risk,
            focus=focus,
            prefer_codes=prefer_codes,
            avoid_codes=avoid_codes,
            base_invest_ratio=base_ratio,
            date_str=requested,
        )
    else:
        # No ranked list — scale competition then trim by risk
        base = _load_submit(requested) or list((kb or {}).get("competition_output") or [])
        holdings = _fallback_scale_competition(base, float(capital), requested, risk)
        alloc_meta = {"mode": "scaled_competition_fallback"}

    # Attach KB reasons / news when available
    positions_meta = {str(p["symbol"]).zfill(6): p for p in (kb or {}).get("positions") or []}
    for h in holdings:
        meta = positions_meta.get(h["symbol"]) or {}
        if meta.get("related_news"):
            h["related_news"] = meta["related_news"][:3]
        if meta.get("reason") and (not h.get("reason") or "综合评分" in str(h.get("reason"))):
            h["reason"] = str(meta["reason"]).replace("LLM: ", "").strip()

    summary = (kb or {}).get("decision_summary_zh") or "基于当日市场与新闻综合判断。"
    if used_fallback:
        pass

    advice = {
        "ok": True,
        "need_run": False,
        "date": requested,
        "used_fallback": used_fallback,
        "mode": "personal",
        "capital": int(capital),
        "risk_preference": risk,
        "focus": focus,
        "prefer_codes": prefer_codes,
        "avoid_codes": avoid_codes,
        "base_capital": int(COMPETITION_CAPITAL),
        "is_empty": len(holdings) == 0,
        "holdings": holdings,
        "decision_summary_zh": summary,
        "market_context_zh": (kb or {}).get("market_context_zh") or "",
        "risk_note": risk_position_note(risk, holdings, float(capital), focus),
        "alloc_meta": alloc_meta,
        "disclaimer": DISCLAIMER,
        "personalization_note": (
            f"已按风格「{RISK_LABELS.get(risk)}」与方向「{FOCUS_LABELS.get(focus)}」"
            "在当日候选池中重新选股与分配仓位，不是简单复制比赛持仓。"
        ),
    }
    return advice


def format_advice_markdown(advice: dict[str, Any]) -> str:
    if not advice.get("ok"):
        return advice.get("error") or "暂无法生成建议。"

    date = advice.get("date", "")
    capital = int(advice.get("capital") or 0)
    risk = RISK_LABELS.get(str(advice.get("risk_preference") or ""), "")
    focus = FOCUS_LABELS.get(str(advice.get("focus") or ""), "")
    lines = [
        f"**{date} 个人 ETF 配置建议**（资金 {capital:,} 元 · {risk} · {focus}）",
        "",
    ]
    if advice.get("live"):
        lines.append("**生成方式：** 基于当日基础数据现算（非直接读取比赛预测结果）。")
        lines.append("")
    if advice.get("personalization_note"):
        lines.append(advice["personalization_note"])
        lines.append("")
    if advice.get("used_fallback") and not advice.get("live"):
        lines.append(f"说明：今日预测尚未生成，以下基于最近交易日 **{date}** 的候选池。")
        lines.append("若需要最新预测，请说「测一下今天」。")
        lines.append("")
    if advice.get("is_empty"):
        lines.append("按您的条件，今日建议 **空仓**（不买入），优先观望。")
    else:
        lines.append(advice.get("risk_note") or "")
        lines.append("")
        for h in advice.get("holdings") or []:
            name = h.get("symbol_name") or h.get("symbol")
            vol = int(h.get("volume") or 0)
            extra = ""
            if h.get("approx_amount"):
                extra = f"，约 {h['approx_amount']:,.0f} 元（{h.get('weight_pct', 0)}%）"
            lines.append(f"- **{name}（{h['symbol']}）** × {vol:,} 股{extra}")
            reason = (h.get("reason") or "").strip()
            if reason:
                lines.append(f"  - 理由：{reason[:140]}")
        lines.append("")
        summary = advice.get("decision_summary_zh") or ""
        if summary:
            lines.append(f"**当日市场观点：** {summary}")

    lines.append("")
    lines.append(DISCLAIMER)
    lines.append("")
    lines.append("不满意可说「重新配置」并换风格/方向，或直接说「偏好红利ETF / 不要黄金」。")
    return "\n".join(lines)


def advice_ui_blocks(advice: dict[str, Any]) -> list[dict[str, Any]]:
    if not advice.get("ok"):
        return []
    return [
        {
            "type": "advice_card",
            "date": advice.get("date"),
            "capital": advice.get("capital"),
            "risk_preference": advice.get("risk_preference"),
            "focus": advice.get("focus"),
            "is_empty": advice.get("is_empty"),
            "holdings": advice.get("holdings") or [],
            "summary": advice.get("decision_summary_zh") or "",
            "risk_note": advice.get("risk_note") or "",
            "disclaimer": DISCLAIMER,
        }
    ]


def competition_ui_blocks(holdings: list[dict[str, Any]], date_str: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "json_block",
            "title": f"{date_str} 比赛提交格式",
            "data": holdings,
            "hint": "比赛资金固定 50 万；可直接复制提交。",
        },
        {
            "type": "advice_card",
            "date": date_str,
            "capital": int(COMPETITION_CAPITAL),
            "risk_preference": "competition",
            "is_empty": len(holdings) == 0,
            "holdings": [
                {
                    "symbol": h.get("symbol"),
                    "symbol_name": h.get("symbol_name"),
                    "volume": h.get("volume"),
                }
                for h in holdings
            ],
            "summary": "比赛模式输出（固定 50 万本金，不受个人偏好影响）",
            "risk_note": "",
            "disclaimer": DISCLAIMER,
        },
    ]
