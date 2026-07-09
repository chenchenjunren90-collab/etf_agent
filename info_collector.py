"""Structured info collection wizard for personal ETF advice."""

from __future__ import annotations

import re
from typing import Any


# Collection field order for personal advice
COLLECT_FIELDS = ("capital", "risk_preference", "focus")

FIELD_META: dict[str, dict[str, Any]] = {
    "capital": {
        "question": "您可用于本次 ETF 配置的资金大约多少？",
        "hint": "个人建议会按您的资金与偏好重新配置；比赛模式仍固定 50 万。",
        "type": "choices_or_input",
        "options": [
            {"label": "10 万以下", "value": 80000},
            {"label": "10–30 万", "value": 200000},
            {"label": "30–50 万", "value": 400000},
            {"label": "50 万（比赛同款）", "value": 500000},
            {"label": "自定义输入", "value": "__custom__"},
        ],
        "input_type": "number",
        "placeholder": "例如 150000",
        "min": 10000,
        "max": 5000000,
    },
    "risk_preference": {
        "question": "您更倾向哪种风格？",
        "hint": "会改变仓位高低、持仓只数，以及更偏防守还是成长。",
        "type": "choices",
        "options": [
            {"label": "稳健（低仓位、偏防守）", "value": "conservative"},
            {"label": "均衡（中等仓位）", "value": "balanced"},
            {"label": "进取（更高仓位、可含成长）", "value": "aggressive"},
        ],
    },
    "focus": {
        "question": "您更关注哪类方向？",
        "hint": "会在当日候选池里倾斜选股；仍只从可交易 ETF 池中选择。",
        "type": "choices",
        "options": [
            {"label": "跟随当日策略（默认）", "value": "auto"},
            {"label": "防守红利 / 高股息", "value": "dividend"},
            {"label": "宽基指数（沪深300等）", "value": "broad"},
            {"label": "成长进攻（创业板/科创）", "value": "growth"},
            {"label": "行业主题（券商/医药等）", "value": "sector"},
        ],
    },
}

RISK_LABELS = {
    "conservative": "稳健",
    "balanced": "均衡",
    "aggressive": "进取",
}

FOCUS_LABELS = {
    "auto": "跟随策略",
    "dividend": "防守红利",
    "broad": "宽基指数",
    "growth": "成长进攻",
    "sector": "行业主题",
}

# Phrases that start personal advice collection
ADVICE_START_HINTS = (
    "投资建议", "怎么投", "怎么配置", "帮我配置", "今天买什么",
    "今日买什么", "给我建议", "个人建议", "帮我看看", "该买什么",
    "推荐什么", "推荐哪些", "配置建议", "资金怎么配", "帮我做个配置",
    "我想投资", "想买etf", "想买ETF", "今日投资建议", "今天投资建议",
    "重新配置", "换个配置", "按我的偏好", "根据我的情况",
)

# Explicit competition mode (skip personal capital collection)
COMPETITION_MODE_HINTS = (
    "今日比赛", "今天比赛", "比赛指令", "比赛预测", "比赛格式",
    "提交格式", "提交指令", "比赛提交", "比赛json", "比赛 JSON",
)


def wants_personal_advice(message: str) -> bool:
    msg = (message or "").strip()
    if not msg:
        return False
    if any(h in msg for h in COMPETITION_MODE_HINTS):
        return False
    return any(h in msg for h in ADVICE_START_HINTS)


def wants_competition_mode(message: str) -> bool:
    return any(h in (message or "") for h in COMPETITION_MODE_HINTS)


def missing_fields(profile: dict[str, Any]) -> list[str]:
    missing = []
    for f in COLLECT_FIELDS:
        if f not in profile or profile[f] in (None, "", "__custom__"):
            missing.append(f)
    return missing


def next_field(profile: dict[str, Any]) -> str | None:
    miss = missing_fields(profile)
    return miss[0] if miss else None


def ui_block_for_field(field: str) -> dict[str, Any]:
    meta = FIELD_META[field]
    block: dict[str, Any] = {
        "type": "choices" if meta["type"] == "choices" else "choices_or_input",
        "field": field,
        "question": meta["question"],
        "hint": meta.get("hint", ""),
        "options": meta.get("options", []),
    }
    if meta.get("input_type"):
        block["input_type"] = meta["input_type"]
        block["placeholder"] = meta.get("placeholder", "")
        block["min"] = meta.get("min")
        block["max"] = meta.get("max")
    return block


def parse_capital(raw: Any) -> int | None:
    """Parse user capital from number, Chinese, or option value."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = int(raw)
        return val if 10000 <= val <= 5000000 else None
    s = str(raw).strip().replace(",", "").replace("，", "").replace(" ", "")
    if not s or s == "__custom__":
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)\s*万$", s)
    if m:
        val = int(float(m.group(1)) * 10000)
        return val if 10000 <= val <= 5000000 else None
    m = re.match(r"^(\d+(?:\.\d+)?)$", s)
    if m:
        val = int(float(m.group(1)))
        if val < 1000:
            val *= 10000
        return val if 10000 <= val <= 5000000 else None
    return None


def parse_risk(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    mapping = {
        "conservative": "conservative",
        "稳健": "conservative",
        "保守": "conservative",
        "防守": "conservative",
        "低风险": "conservative",
        "balanced": "balanced",
        "均衡": "balanced",
        "默认": "balanced",
        "aggressive": "aggressive",
        "进取": "aggressive",
        "激进": "aggressive",
        "高风险": "aggressive",
    }
    if s in mapping:
        return mapping[s]
    # Prefer more specific / longer keys first when scanning free text
    for key in ("低风险", "高风险", "conservative", "aggressive", "balanced",
                "稳健", "保守", "进取", "激进", "均衡", "防守"):
        if key in s:
            return mapping[key]
    return None


def parse_focus(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    mapping = {
        "auto": "auto",
        "跟随": "auto",
        "跟随策略": "auto",
        "默认": "auto",
        "dividend": "dividend",
        "红利": "dividend",
        "防守红利": "dividend",
        "高股息": "dividend",
        "股息": "dividend",
        "broad": "broad",
        "宽基": "broad",
        "指数": "broad",
        "沪深300": "broad",
        "growth": "growth",
        "成长": "growth",
        "进攻": "growth",
        "创业板": "growth",
        "科创": "growth",
        "科技": "growth",
        "sector": "sector",
        "行业": "sector",
        "主题": "sector",
        "券商": "sector",
        "医药": "sector",
        "证券": "sector",
    }
    if s in mapping:
        return mapping[s]
    for k, v in mapping.items():
        if k in s:
            return v
    return None


def apply_field_answer(
    profile: dict[str, Any],
    field: str,
    value: Any,
) -> tuple[dict[str, Any], str | None]:
    profile = dict(profile or {})
    if field == "capital":
        cap = parse_capital(value)
        if cap is None:
            return profile, "请输入 1 万～500 万之间的金额，或点选上方选项。"
        profile["capital"] = cap
        return profile, None
    if field == "risk_preference":
        risk = parse_risk(value)
        if risk is None:
            return profile, "请选择：稳健 / 均衡 / 进取。"
        profile["risk_preference"] = risk
        return profile, None
    if field == "focus":
        focus = parse_focus(value)
        if focus is None:
            return profile, "请选择关注方向，或点选上方选项。"
        profile["focus"] = focus
        return profile, None
    return profile, f"未知字段：{field}"


def try_extract_from_message(message: str, profile: dict[str, Any]) -> dict[str, Any]:
    """Best-effort extract capital/risk/focus from free text."""
    profile = dict(profile or {})
    msg = message or ""

    if "capital" not in profile:
        m = re.search(
            r"(?:资金|本金|有|投入|可用)[^\d]{0,6}(\d+(?:\.\d+)?)\s*万",
            msg,
        )
        if m:
            cap = parse_capital(f"{m.group(1)}万")
            if cap:
                profile["capital"] = cap
        else:
            m2 = re.search(r"(\d{4,7})\s*元?", msg)
            if m2:
                cap = parse_capital(m2.group(1))
                if cap:
                    profile["capital"] = cap

    if "risk_preference" not in profile:
        if any(w in msg for w in ("稳健", "保守", "防守", "低风险")):
            profile["risk_preference"] = "conservative"
        elif any(w in msg for w in ("进取", "激进", "高风险", "进攻型")):
            profile["risk_preference"] = "aggressive"
        elif any(w in msg for w in ("均衡", "中性", "适中")):
            profile["risk_preference"] = "balanced"

    if "focus" not in profile:
        focus = parse_focus(msg)
        # Only set if message clearly mentions a direction keyword
        if focus and focus != "auto":
            profile["focus"] = focus
        elif any(w in msg for w in ("跟随策略", "按策略", "你看着配")):
            profile["focus"] = "auto"
    else:
        # Allow overriding an existing focus when user clearly asks to change
        if any(w in msg for w in ("改成", "换成", "改为", "调整为", "换成", "偏好", "只要")):
            focus = parse_focus(msg)
            if focus:
                profile["focus"] = focus
        elif any(w in msg for w in ("红利", "宽基", "成长", "行业", "防守红利", "创业板", "科创")):
            focus = parse_focus(msg)
            if focus and focus != "auto":
                profile["focus"] = focus

    # Prefer / avoid specific ETF names or codes
    prefer = list(profile.get("prefer_codes") or [])
    avoid = list(profile.get("avoid_codes") or [])
    from agent_kb import ETF_ALIASES, resolve_etf_code

    # 「想买红利」「偏好证券ETF」
    if any(w in msg for w in ("想买", "偏好", "只要", "重点", "偏向", "关注")):
        code = resolve_etf_code(msg)
        if code and code not in prefer:
            prefer.append(code)
    # 「不要黄金」「避开医药」
    if any(w in msg for w in ("不要", "别买", "避开", "排除", "不想要")):
        # try each alias mentioned
        for alias, code in ETF_ALIASES.items():
            if alias and len(alias) >= 2 and alias in msg:
                if code not in avoid:
                    avoid.append(code)
        code = resolve_etf_code(msg)
        if code and code not in avoid:
            avoid.append(code)

    if prefer:
        profile["prefer_codes"] = prefer[:5]
    if avoid:
        profile["avoid_codes"] = avoid[:5]

    return profile


def collection_prompt(field: str) -> str:
    meta = FIELD_META[field]
    lines = [meta["question"], ""]
    if meta.get("hint"):
        lines.append(meta["hint"])
        lines.append("")
    lines.append("请点选下方选项，或直接在输入框回答。")
    return "\n".join(lines)


def ready_summary(profile: dict[str, Any]) -> str:
    cap = int(profile.get("capital") or 0)
    risk = RISK_LABELS.get(str(profile.get("risk_preference") or "balanced"), "均衡")
    focus = FOCUS_LABELS.get(str(profile.get("focus") or "auto"), "跟随策略")
    extra = []
    if profile.get("prefer_codes"):
        extra.append("偏好 " + "、".join(profile["prefer_codes"]))
    if profile.get("avoid_codes"):
        extra.append("避开 " + "、".join(profile["avoid_codes"]))
    extra_txt = ("；" + "；".join(extra)) if extra else ""
    return (
        f"已记录：资金 **{cap:,} 元**，风格 **{risk}**，方向 **{focus}**{extra_txt}。\n\n"
        "正在用当日基础数据（行情/新闻/宏观）**现算**您的 ETF 配置"
        "（不改动比赛官方预测）…"
    )


DISCLAIMER = (
    "以上仅供参考，不构成投资建议；市场有风险，决策需自负。"
)
