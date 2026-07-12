"""Theme keyword tables and strict news-quality filters.

The active backtest is still price-only.  The functions in this module provide
the stricter news screening rules requested in ``新闻策略.pdf`` so a future news
source can plug in without bringing back the old crawler/daily-job stack.

Core policy:
  1. Prefer concrete, quantifiable catalysts over vague optimism.
  2. Require a clear sector / ETF mapping before giving a strong score.
  3. Penalize late news after a long rally and good news in a long downtrend.
"""

from __future__ import annotations

import hashlib
from typing import Any

# Per-ETF keyword weights — used only for tagging hot picks in reports.
ETF_THEME_KEYWORDS: dict[str, dict[str, float]] = {
    "510050": {"上证50": 0.9, "大蓝筹": 0.5, "权重": 0.35, "银行": 0.25, "保险": 0.25},
    "510300": {"沪深300": 0.9, "沪指": 0.45, "大盘": 0.4, "宽基": 0.3, "A股": 0.25},
    "510330": {"沪深300": 0.85, "沪指": 0.4, "大盘": 0.35},
    "510500": {"中证500": 0.85, "中盘": 0.45, "中小盘": 0.35},
    # 宽基 + 商品避险
    "159338": {"中证A500": 0.9, "A500": 0.85, "宽基": 0.4, "大盘": 0.35, "A股": 0.25},
    "518880": {
        "黄金": 0.9, "金价": 0.7, "避险": 0.7, "美元": 0.3, "通胀": 0.45,
        "地缘": 0.45, "美联储": 0.35, "FOMC": 0.35, "国债收益率": 0.25,
    },
    "159985": {
        "豆粕": 0.95, "大豆": 0.7, "饲料": 0.5, "农产品": 0.4,
        "厄尔尼诺": 0.5, "干旱": 0.4, "USDA": 0.45,
    },
    "159915": {"创业板": 0.9, "创指": 0.55},
    "588000": {"科创板": 0.9, "科创50": 0.85, "硬科技": 0.45, "芯片": 0.4, "半导体": 0.45},
    "512100": {"中证1000": 0.85, "小盘": 0.4, "专精特新": 0.35},
    "159845": {"中证1000": 0.85, "小盘": 0.4},
    "510880": {"红利": 0.85, "高股息": 0.7, "煤炭": 0.35, "公用事业": 0.25},
    "512880": {"券商": 0.9, "证券": 0.85, "投行": 0.35, "成交": 0.2},
    "512690": {"白酒": 0.85, "消费": 0.45, "茅台": 0.5, "食品饮料": 0.35},
    "159949": {"创业板50": 0.85, "创业板": 0.55, "创蓝筹": 0.5},
    "512010": {"医药": 0.85, "医疗": 0.75, "创新药": 0.55, "CXO": 0.35, "医保": 0.2},
    "515790": {"光伏": 0.9, "新能源": 0.55, "储能": 0.35, "锂电": 0.25},
    "516510": {"云计算": 0.75, "AI": 0.65, "人工智能": 0.6, "算力": 0.55, "数据中心": 0.35, "软件": 0.3},
}

CONCRETE_CATALYST_KEYWORDS: dict[str, tuple[str, ...]] = {
    "policy_landing": (
        "发布", "落地", "实施", "细则", "方案", "通知", "补贴", "扶持", "解禁", "关税",
        "降准", "降息", "专项债", "产业政策", "四部门", "工信部", "发改委",
    ),
    "capital_investment": (
        "大额投资", "计划融资", "完成融资", "融资", "募资", "并购", "收购", "重组",
        "定增", "扩产", "投产", "开工", "签约",
    ),
    "orders_and_delivery": (
        "订单", "中标", "招标", "交付", "列装", "装机", "采购", "合同", "供应",
        "量产", "出货",
    ),
    "earnings": (
        "业绩", "预增", "利润", "净利润", "营收", "业绩增长", "营收增长", "利润增长",
        "同比增长", "环比增长", "暴增", "翻倍", "扭亏", "超预期",
    ),
    "technology_breakthrough": (
        "突破", "首个", "首次", "国产替代", "认证", "获批", "上市", "临床", "专利",
        "技术突破", "装备", "航母",
    ),
    "fund_flow": (
        "净流入", "大幅净流入", "回购", "增持", "北向", "ETF份额", "放量",
    ),
}

NEGATIVE_CATALYST_HINTS = (
    "利空", "减持", "预亏", "亏损", "暴雷", "下滑", "处罚", "调查", "制裁", "禁令",
    "断供", "召回", "取消订单", "解约", "关税上调", "跌停",
)

VAGUE_NEWS_HINTS = (
    "前景广阔", "前景可期", "未来发展", "发展向好", "空间巨大", "有望受益",
    "持续景气", "爆发式增长", "技术壁垒", "商业化提速", "迎来机遇",
)

# 标题包含任意一个时直接拒绝：这是程序自动生成的数据快讯，不是 PDF 中的实质事件。
DATA_TICK_TITLE_HINTS = (
    "资金榜", "主力榜", "融资榜", "净流入榜", "净流出榜", "两融余额",
    "ETF份额", "份额变动", "持仓变动", "盘中异动", "成交额榜",
    "资金流向", "融资融券余额",
)

ETF_THEME_KEYWORDS.update({
    "515790": {
        **ETF_THEME_KEYWORDS["515790"],
        "绿电": 0.45,
        "电力": 0.25,
        "储能电芯": 0.40,
    },
    "516510": {
        **ETF_THEME_KEYWORDS["516510"],
        "DeepSeek": 0.65,
        "大模型": 0.55,
        "AI芯片": 0.55,
        "服务器": 0.35,
    },
    "588000": {
        **ETF_THEME_KEYWORDS["588000"],
        "AI芯片": 0.45,
        "国产替代": 0.35,
    },
    # 宏观经济触发词：让"经济日历 / 新闻联播 / 东财"里的政策与数据信号都能落到大盘 ETF 上。
    # 同一组词对多个源公平生效，便于做 A/B/C 三组对比。
    "510300": {
        **ETF_THEME_KEYWORDS["510300"],
        "M2": 0.45, "社融": 0.45, "信贷": 0.35,
        "CPI": 0.45, "PMI": 0.45, "LPR": 0.45,
        "降准": 0.55, "降息": 0.5, "MLF": 0.35,
        "财政政策": 0.35, "货币政策": 0.4, "央行": 0.25,
    },
    "510050": {
        **ETF_THEME_KEYWORDS["510050"],
        "M2": 0.35, "PMI": 0.4, "降准": 0.45,
    },
    "512880": {
        **ETF_THEME_KEYWORDS["512880"],
        "降准": 0.45, "降息": 0.4, "LPR": 0.35,
    },
    "510880": {
        **ETF_THEME_KEYWORDS["510880"],
        "降息": 0.3, "LPR": 0.3,
    },
})

STRONG_NEWS_THRESHOLD = 0.35
WEAK_NEWS_THRESHOLD = 0.12


def max_abs_theme_score(theme_scores: dict[str, Any] | None) -> float:
    if not theme_scores:
        return 0.0
    return float(max(abs(float(v)) for v in theme_scores.values()))


def _article_text(article: dict[str, Any]) -> str:
    return " ".join(
        str(article.get(k) or "")
        for k in ("title", "content", "summary", "digest", "keywords")
    )


def _hit_words(text: str, words: tuple[str, ...]) -> list[str]:
    return [w for w in words if w and w in text]


def _theme_scores_from_text(text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    for code, keywords in ETF_THEME_KEYWORDS.items():
        raw = 0.0
        for keyword, weight in keywords.items():
            if keyword in text:
                raw += float(weight)
        if raw > 0:
            scores[code] = round(min(raw, 1.5), 3)
    return scores


def _concrete_event_hits(text: str) -> dict[str, list[str]]:
    return {
        category: _hit_words(text, keywords)
        for category, keywords in CONCRETE_CATALYST_KEYWORDS.items()
        if _hit_words(text, keywords)
    }


def _trend_penalty(code: str, trend_context: dict[str, Any] | None) -> tuple[float, list[str]]:
    """Apply PDF exclusion rules using optional pre-news price context.

    ``trend_context`` may be shaped as ``{code: features}`` or as one feature
    dict for the current code.  Supported keys: ``ret_5d``, ``ret_10d``,
    ``ret_20d``, ``consecutive_up_days``, ``trend_score``.
    """
    if not trend_context:
        return 1.0, []

    features = trend_context.get(code, trend_context) if isinstance(trend_context, dict) else {}
    if not isinstance(features, dict):
        return 1.0, []

    penalty = 1.0
    flags: list[str] = []
    ret_5d = float(features.get("ret_5d", 0.0) or 0.0)
    ret_10d = float(features.get("ret_10d", 0.0) or 0.0)
    ret_20d = float(features.get("ret_20d", 0.0) or 0.0)
    up_days = int(features.get("consecutive_up_days", 0) or 0)
    trend_score = float(features.get("trend_score", 50.0) or 50.0)

    if up_days >= 5 or ret_5d >= 8.0 or ret_10d >= 12.0:
        penalty *= 0.35
        flags.append("late_news_after_rally")

    if ret_20d <= -8.0 or trend_score < 42.0:
        penalty *= 0.45
        flags.append("good_news_in_downtrend")

    return penalty, flags


def score_news_article(
    article: dict[str, Any],
    *,
    trend_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one news item under the stricter screening policy.

    Returns a structured decision with:
    - ``accepted``: whether the item may affect ETF scores.
    - ``quality``: ``strong`` / ``weak`` / ``rejected``.
    - ``reason``: why the item was accepted or rejected.
    - ``theme_scores``: ETF-level signed signal contribution.
    """
    text = _article_text(article)
    title = str(article.get("title") or "")
    if not text.strip():
        return {
            "accepted": False,
            "title": title,
            "source": str(article.get("source") or ""),
            "quality": "rejected",
            "reason": "empty_article",
            "event_hits": {},
            "theme_scores": {},
            "risk_flags": [],
        }

    if any(hint in title for hint in DATA_TICK_TITLE_HINTS):
        return {
            "accepted": False,
            "title": title,
            "source": str(article.get("source") or ""),
            "quality": "rejected",
            "reason": "data_tick_not_event",
            "event_hits": {},
            "theme_scores": {},
            "risk_flags": [],
        }

    event_hits = _concrete_event_hits(text)
    vague_hits = _hit_words(text, VAGUE_NEWS_HINTS)
    negative_hits = _hit_words(text, NEGATIVE_CATALYST_HINTS)
    theme_hits = _theme_scores_from_text(text)

    if not theme_hits:
        return {
            "accepted": False,
            "title": str(article.get("title") or ""),
            "source": str(article.get("source") or ""),
            "quality": "rejected",
            "reason": "no_clear_sector_or_etf_mapping",
            "event_hits": event_hits,
            "theme_scores": {},
            "risk_flags": [],
        }

    if not event_hits:
        if vague_hits:
            base_quality = "weak"
            base_score = 0.16
            reason = "vague_theme_only"
        else:
            return {
                "accepted": False,
                "title": str(article.get("title") or ""),
                "source": str(article.get("source") or ""),
                "quality": "rejected",
                "reason": "no_concrete_catalyst",
                "event_hits": event_hits,
                "theme_scores": {},
                "risk_flags": [],
            }
    else:
        event_strength = min(1.0, 0.25 + 0.18 * sum(len(v) for v in event_hits.values()))
        # 两级强度划分：
        # strong: 命中催化剂且事件强度>=STRONG_NEWS_THRESHOLD（有具体数据/政策支撑）
        # weak: 命中催化剂但无具体量化数据（由if not event_hits分支处理）
        base_score = min(0.42, max(STRONG_NEWS_THRESHOLD, event_strength))
        base_quality = "strong"
        reason = "concrete_catalyst_and_clear_sector"

    sign = -1.0 if negative_hits else 1.0
    scored_themes: dict[str, float] = {}
    risk_flags: list[str] = []
    for code, theme_strength in theme_hits.items():
        penalty, flags = _trend_penalty(code, trend_context)
        risk_flags.extend(flags)
        score = sign * base_score * max(0.75, min(1.0, theme_strength)) * penalty
        if abs(score) >= WEAK_NEWS_THRESHOLD:
            scored_themes[code] = round(float(max(-0.85, min(0.85, score))), 3)

    if not scored_themes:
        return {
            "accepted": False,
            "title": str(article.get("title") or ""),
            "source": str(article.get("source") or ""),
            "quality": "rejected",
            "reason": "filtered_by_price_context",
            "event_hits": event_hits,
            "theme_scores": {},
            "risk_flags": sorted(set(risk_flags)),
        }

    if risk_flags and base_quality == "strong":
        base_quality = "weak"
        reason = f"{reason}_but_price_risk"

    content = str(article.get("content") or "")
    return {
        "accepted": True,
        "title": str(article.get("title") or ""),
        "source": str(article.get("source") or ""),
        "url": str(article.get("url") or ""),
        "published_at": str(article.get("published_at") or ""),
        "fetched_at": str(article.get("fetched_at") or ""),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "quality": base_quality,
        "reason": reason,
        "event_hits": event_hits,
        "vague_hits": vague_hits,
        "negative_hits": negative_hits,
        "theme_scores": scored_themes,
        "risk_flags": sorted(set(risk_flags)),
    }


def build_news_signal(
    articles: list[dict[str, Any]],
    *,
    trend_context: dict[str, Any] | None = None,
    date: str | None = None,
) -> dict[str, Any]:
    """Aggregate screened articles into ETF-level theme scores.

    聚合规则：每只 ETF 只取 |贡献分| 最大的前 3 条，按 1.0/0.5/0.25 权重合成。
    此前是全量累加再 clamp，±0.85 上限在新闻量大时天天全池顶格
    （85 日回测实测 85/85 天有 ≥6 只 ETF 饱和），新闻层完全失去横截面
    区分度；top-3 衰减合成让「一条重磅」仍能打满、大量弱提及不再饱和。
    """
    contrib: dict[str, list[float]] = {code: [] for code in ETF_THEME_KEYWORDS}
    accepted = []
    rejected = []

    for article in articles:
        scored = score_news_article(article, trend_context=trend_context)
        if scored["accepted"]:
            accepted.append(scored)
            for code, value in scored["theme_scores"].items():
                contrib.setdefault(code, []).append(float(value))
        else:
            rejected.append(scored)

    _TOPK_WEIGHTS = (1.0, 0.5, 0.25)
    total_scores: dict[str, float] = {}
    for code, values in contrib.items():
        top = sorted(values, key=abs, reverse=True)[: len(_TOPK_WEIGHTS)]
        total_scores[code] = sum(v * w for v, w in zip(top, _TOPK_WEIGHTS))

    theme_scores = {
        code: round(float(max(-0.85, min(0.85, value))), 3)
        for code, value in total_scores.items()
        if abs(value) >= WEAK_NEWS_THRESHOLD
    }
    strong_count = sum(1 for item in accepted if item.get("quality") == "strong")
    weak_count = sum(1 for item in accepted if item.get("quality") == "weak")
    confidence = min(1.0, 0.20 * strong_count + 0.04 * weak_count)
    market_refs = ("510300", "510050", "510500")
    market_values = [theme_scores[c] for c in market_refs if c in theme_scores]
    market_sentiment = round(float(sum(market_values) / len(market_values)), 3) if market_values else 0.0
    catalyst_hits = sum(
        sum(len(v) for v in (item.get("event_hits") or {}).values())
        for item in accepted
    )
    hot_keywords = []
    for code, score in sorted(theme_scores.items(), key=lambda kv: abs(kv[1]), reverse=True):
        hot_keywords.extend(list(ETF_THEME_KEYWORDS.get(code, {}).keys())[:2])

    return {
        "date": date,
        "source": "strict_news_filter",
        "confidence": round(float(confidence), 3),
        "market_sentiment": market_sentiment,
        "hot_keywords": list(dict.fromkeys(hot_keywords))[:12],
        "theme_scores": theme_scores,
        "article_count": len(articles),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "strong_count": strong_count,
        "weak_count": weak_count,
        "catalyst_hits": int(catalyst_hits),
        "max_abs_theme": round(max_abs_theme_score(theme_scores), 3),
        "accepted_articles": accepted,
        "provenance": {
            "accepted_with_published_at": sum(
                1 for item in accepted if item.get("published_at")
            ),
            "accepted_total": len(accepted),
        },
        "rejection_reasons": {
            reason: sum(1 for item in rejected if item.get("reason") == reason)
            for reason in sorted({item.get("reason") for item in rejected})
        },
    }


# ---------------------------------------------------------------------------
# LLM-facing helpers
# ---------------------------------------------------------------------------

def _article_signed_score(article: dict[str, Any]) -> float:
    """Return the largest absolute theme score (signed) for ranking."""
    scores = article.get("theme_scores") or {}
    if not scores:
        return 0.0
    best = max(scores.values(), key=lambda v: abs(float(v)))
    return float(best)


def summarize_for_llm(
    signal: dict[str, Any],
    *,
    max_items: int = 20,
) -> list[dict[str, Any]]:
    """Compress the news signal into a compact list the LLM can consume.

    Strong/weak articles ranked by |theme_score| descending, capped at
    ``max_items``.  Each entry keeps only what the model needs to reason about
    direction, confidence and ETF mapping.
    """
    accepted = list(signal.get("accepted_articles") or [])
    accepted.sort(key=lambda a: (
        0 if a.get("quality") == "strong" else 1,
        -abs(_article_signed_score(a)),
    ))
    summarised: list[dict[str, Any]] = []
    for article in accepted[:max_items]:
        events = article.get("event_hits") or {}
        event_tags = sorted({cat for cat, hits in events.items() if hits})
        theme_scores = {
            code: round(float(v), 3)
            for code, v in (article.get("theme_scores") or {}).items()
        }
        summarised.append({
            "title": str(article.get("title") or "")[:120],
            "source": str(article.get("source") or "")[:32],
            "quality": str(article.get("quality") or ""),
            "event_tags": event_tags,
            "etf_scores": theme_scores,
            "negative": bool(article.get("negative_hits")),
            "risk_flags": list(article.get("risk_flags") or []),
        })
    return summarised


def render_news_for_prompt(summary: list[dict[str, Any]]) -> str:
    """Render the LLM-facing summary as a readable Markdown block."""
    if not summary:
        return "（开盘前未抓到通过严格规则的新闻；判断主要依赖经济日历与价量。）"
    lines = []
    for idx, item in enumerate(summary, 1):
        direction = "(-)" if item.get("negative") else "(+)"
        etf_part = ", ".join(
            f"{c}:{v:+.2f}" for c, v in (item.get("etf_scores") or {}).items()
        ) or "无"
        events_part = "/".join(item.get("event_tags") or []) or "无"
        risk_part = (" | risk=" + ",".join(item["risk_flags"])) if item.get("risk_flags") else ""
        lines.append(
            f"{idx}. [{item.get('quality', '')}{direction}] "
            f"{item.get('title', '')} "
            f"(src={item.get('source', '')}; events={events_part}; etf={etf_part}{risk_part})"
        )
    return "\n".join(lines)
