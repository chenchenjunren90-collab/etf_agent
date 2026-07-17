"""Grounded financial-event extraction via DeepSeek.

Two-stage architecture:
  Stage 1 (fast): rule hits plus high-recall event-language retrieval.
  Stage 2 (precise): extract structured events from bounded source excerpts,
    require verbatim evidence, and map only grounded events to candidate ETFs.

Anti-hallucination measures:
  - Prompt constrains output to only ETF codes in the candidate pool
  - Requires structured JSON with reason for each judgment
  - Relevance < 0.3 is auto-discarded regardless of sentiment
  - Single article capped at max 3 ETFs to prevent over-propagation
  - Fallback: if LLM fails, falls back to keyword scores (graceful degradation)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any

from llm_client import is_available, call_json, LLMUnavailable, LLMResponseError

# ── 配置 ──────────────────────────────────────────
LLM_NEWS_MODEL = os.environ.get("LLM_NEWS_MODEL", "deepseek-chat")
LLM_NEWS_TEMPERATURE = 0.15  # 低温度：要求判断一致稳定
MAX_ARTICLES_PER_CALL = 15    # 单次LLM调用最多处理的文章数
MAX_ETFS_PER_ARTICLE = 3      # 单条新闻最多影响3只ETF
RELEVANCE_FLOOR = 0.30        # 低于此值直接丢弃

# ── Prompt 模板 ─────────────────
# 基于CSDN四年765天新闻-价格回测得出以下铁律：
# 1. 新闻情绪单独预测力≈0 (corr=-0.03)
# 2. 逆势利好是毒药：跌势+正面新闻 → 次日-0.14%
# 3. 共振勉强有效：涨势+正面新闻 → 次日+0.01%
# 结论：新闻只能做趋势的增强器，不能单独决策
_LEGACY_DIRECTIONAL_SYSTEM_PROMPT = """你是专业金融新闻语义分析系统。你的输出将被量化策略直接使用。

=== 核心铁律（来自四年数据回测） ===
1. 新闻不能逆转趋势。当市场已在下行，利好新闻基本无效甚至有害。
2. 新闻只能增强趋势。上涨趋势中正面新闻有微弱正面效果（+0.01%），下跌趋势中正面新闻大概率是陷阱（-0.14%）。
3. 高情绪+连涨3日以上→大概率已定价，追高反而风险大。
4. 新闻分数的价值在于"去伪存真"——剔除假信号比找到真信号更重要。

=== 候选ETF（在用户消息中提供）===

=== 判断流程（严格顺序，不可跳过）===

【第一步：趋势感知】用户消息中会提供每只ETF的近5日涨跌。如果该ETF近5日跌超1%，你对该ETF的任何positive判断都应极度谨慎，relevance自动×0.5。

【第二步：否定句检测（最容易出错的点）】
在判断sentiment前先检测否定结构：
- "不会降准" / "没有加息" / "可能不" → 虽含正向关键词，实际负向或中性
- "可能降准" ≠ "宣布降准" — "可能"未发生，relevance≤0.3
- "有望受益" / "或将迎来" / "预计将" → 预测性语言，relevance≤0.2
- "如果...那么..." → 条件句，relevance≤0.2

【第三步：数据快讯/空泛拒绝（直接relevance=0）】
- 资金流向汇总、龙虎榜、ETF份额、两融余额
- "前景广阔""空间巨大""有望受益""或将迎来""行业景气"
- 券商"维持买入评级"（无新信息）
- 仅含价格播报无原因说明

【第四步：催化剂校验（7类，必须有正反例判断）】
只有包含以下之一才给relevance>0.3：
1. policy_landing: 具体政策发布→例"央行降准0.5%"；反例"市场预期降准"
2. earnings: 具体业绩→例"净利润增30%"；反例"分析师预计"
3. macro_signal: 宏观数据公布→例"CPI同比3.5%"；反例"关注今晚数据"
4. capital_investment: 具体投资→例"10亿扩产"；反例"投资有望加码"
5. orders: 具体订单→例"中标50亿"；反例"订单预计增长"
6. tech_breakthrough: 具体突破→例"新药获批"；反例"技术壁垒有望突破"
7. fund_flow: 具体行动→例"国家队增持"；反例"资金有望流入"

【第五步：评分校准】
relevance:
- 标题提及核心关键词+有具体催化剂+趋势配合 → 0.7-1.0
- 正文提及+具体催化剂 → 0.5-0.7
- 仅有板块映射+弱催化剂 → 0.3-0.5
- 趋势不配合(relevance×0.5)：跌势中的利好、涨势中的利空

strength:
- strong: 精确数字+明确主体+已发生+趋势同向 → 0.85+
- moderate: 具体事件但缺数字 → 0.55-0.75
- weak: 间接/氛围影响 → 0.35-0.50

sentiment:
- positive: 已发生的利好事件
- negative: 已发生的利空事件
- neutral: 事件中性、或情绪不明、或预测未发生

=== 输出格式（JSON 对象，键名 articles）===
{"articles": [{"title": "标题前50字", "etf_judgments": [{"code": "510300", "relevance": 0.85, "sentiment": "positive", "strength": "strong", "reason": "引用新闻原句", "catalyst_type": "macro_signal"}]}]}

只输出上述 json，不要 markdown 代码块，不要任何多余文字。"""


# Keep factual extraction independent from the downstream profit objective.
EVENT_EXTRACTION_SYSTEM_PROMPT = """你是金融事件事实抽取器，不是投资顾问。

任务：只根据给出的标题和正文摘录，识别已经发生或已经正式宣布的具体事件，
并判断该事实通过什么经济机制影响候选ETF。不要预测价格，不要给买卖建议，
不要为了寻找盈利机会而放宽事实标准。

硬性规则：
1. evidence 必须逐字摘自输入标题或正文；没有可核验原文就不要输出该事件。
2. event_status 只能是 occurred、announced、forecast、rumor、unclear。
3. scope 只能是 market、sector、multi_company、single_company、unclear。
4. 单家公司财报或订单默认是 single_company，不能伪装成整个ETF事件。
5. novelty 只能是 new、update、repeat、unclear；重复报道不要增强强度。
6. 只允许输出候选池中的ETF代码，每篇最多3只ETF。
7. direction 只能是 positive、negative、neutral；不确定时用 neutral。
8. relevance 表示事实与ETF基本面的直接关联度，不表示涨跌概率。

严格输出 JSON 对象：
{"articles":[{"article_id":"输入ID","event_type":"policy|earnings|macro|orders|capital_investment|technology|regulation|supply|other","event_status":"occurred|announced|forecast|rumor|unclear","novelty":"new|update|repeat|unclear","scope":"market|sector|multi_company|single_company|unclear","event_key":"主体|事件类型|核心事实","entities":["主体"],"evidence":"输入中的连续原文","etf_judgments":[{"code":"510300","relevance":0.80,"direction":"positive","strength":"strong|moderate|weak","transmission":"事实影响ETF的简短机制"}]}]}

没有满足条件的事件时输出 {"articles":[]}。只输出 JSON。"""


def _build_user_prompt(
    articles: list[dict[str, Any]],
    pool_codes: list[str],
) -> str:
    """构建用户prompt：ETF候选池(含行业说明) + 新闻列表。"""
    # ETF描述，帮助LLM理解每只ETF代表什么
    etf_desc = {
        "510300": "沪深300(大盘蓝筹)", "510050": "上证50(超大盘)",
        "510500": "中证500(中盘成长)", "510330": "沪深300(华夏)",
        "159338": "中证A500(全市场)", "518880": "黄金ETF(避险商品)",
        "159985": "豆粕ETF(农产品商品)", "510880": "红利ETF(高股息防御)",
        "512880": "证券ETF(券商周期)", "512010": "医药ETF(医疗)",
        "159915": "创业板(成长)", "588000": "科创50(科技)",
        "159949": "创业板50(创蓝筹)",
    }
    lines = []
    lines.append("=== ETF候选池 ===")
    codes_str = ", ".join(
        f"{c}({etf_desc.get(c, '')})" for c in sorted(pool_codes)
    )
    lines.append(codes_str)
    lines.append("")
    lines.append("=== 待分析新闻（请输出 json） ===")
    for idx, art in enumerate(articles, 1):
        article_id = str(art.get("article_id") or "")
        title = str(art.get("title", ""))[:120]
        content = str(
            art.get("content_excerpt") or art.get("content") or ""
        )[:900]
        source = str(art.get("source", ""))[:32]
        lines.append(f"[{idx}] article_id={article_id} | 来源:{source} | {title}")
        if content:
            lines.append(f"    正文: {content}")
    return "\n".join(lines)


def _parse_llm_response(
    raw: str,
    valid_codes: set[str],
) -> list[dict[str, Any]]:
    """解析LLM返回的JSON，过滤无效输出。"""
    try:
        # 尝试提取JSON数组
        text = raw.strip()
        if text.startswith("```"):
            # 去掉markdown代码块
            lines = text.split("\n")
            start = 1 if lines[0].startswith("```") else 0
            end = len(lines) - 1 if lines[-1] == "```" else len(lines)
            text = "\n".join(lines[start:end])
        data = json.loads(text)
        if not isinstance(data, list):
            return []
        results = []
        for item in data:
            title = str(item.get("title", ""))[:80]
            judgments = item.get("etf_judgments", [])
            if not isinstance(judgments, list):
                continue
            valid_judgments = []
            for j in judgments[:MAX_ETFS_PER_ARTICLE]:
                code = str(j.get("code", "")).strip().zfill(6)
                if code not in valid_codes:
                    continue  # 丢弃非候选池代码（防幻觉）
                relevance = float(j.get("relevance", 0.0) or 0.0)
                if relevance < RELEVANCE_FLOOR:
                    continue  # 低于相关性阈值
                valid_judgments.append({
                    "code": code,
                    "relevance": round(min(relevance, 1.0), 3),
                    "sentiment": str(j.get("sentiment", "neutral")).lower(),
                    "strength": str(j.get("strength", "weak")).lower(),
                    "reason": str(j.get("reason", ""))[:120],
                    "catalyst_type": str(j.get("catalyst_type", "none")).lower(),
                })
            if valid_judgments:
                results.append({"title": title, "etf_judgments": valid_judgments})
        return results
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        print(f"[news_llm] JSON解析失败: {e}")
        return []


def _grounding_text(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _parse_structured_response(
    raw: str,
    valid_codes: set[str],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Parse structured events and reject claims not grounded in source text."""
    try:
        data = json.loads(raw.strip())
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []

    by_id = {
        str(item.get("article_id") or ""): item
        for item in candidates
        if str(item.get("article_id") or "")
    }
    valid_statuses = {"occurred", "announced", "forecast", "rumor", "unclear"}
    valid_novelty = {"new", "update", "repeat", "unclear"}
    valid_scopes = {"market", "sector", "multi_company", "single_company", "unclear"}
    valid_directions = {"positive", "negative", "neutral"}
    valid_strengths = {"strong", "moderate", "weak"}
    parsed: list[dict[str, Any]] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        article_id = str(item.get("article_id") or "").strip()
        candidate = by_id.get(article_id)
        if candidate is None:
            continue
        evidence = str(item.get("evidence") or "").strip()[:240]
        source_text = f"{candidate.get('title', '')} {candidate.get('content_excerpt', '')}"
        grounded_evidence = _grounding_text(evidence)
        grounded = bool(
            len(grounded_evidence) >= 6
            and grounded_evidence in _grounding_text(source_text)
        )
        if not grounded:
            continue

        status = str(item.get("event_status") or "unclear").lower()
        novelty = str(item.get("novelty") or "unclear").lower()
        scope = str(item.get("scope") or "unclear").lower()
        if status not in valid_statuses:
            status = "unclear"
        if novelty not in valid_novelty:
            novelty = "unclear"
        if scope not in valid_scopes:
            scope = "unclear"

        actionable_status = status in {"occurred", "announced"}
        not_repeated = novelty in {"new", "update"}
        etf_wide_scope = scope in {"market", "sector", "multi_company"}
        judgments: list[dict[str, Any]] = []
        raw_judgments = item.get("etf_judgments") or []
        if not isinstance(raw_judgments, list):
            raw_judgments = []
        for judgment in raw_judgments[:MAX_ETFS_PER_ARTICLE]:
            if not isinstance(judgment, dict):
                continue
            code = str(judgment.get("code") or "").strip().zfill(6)
            if code not in valid_codes:
                continue
            try:
                relevance = float(judgment.get("relevance") or 0.0)
            except (TypeError, ValueError):
                continue
            relevance = max(0.0, min(1.0, relevance))
            direction = str(
                judgment.get("direction") or judgment.get("sentiment") or "neutral"
            ).lower()
            strength = str(judgment.get("strength") or "weak").lower()
            if direction not in valid_directions:
                direction = "neutral"
            if strength not in valid_strengths:
                strength = "weak"
            direct_evidence = bool(
                actionable_status
                and not_repeated
                and etf_wide_scope
                and direction != "neutral"
                and relevance >= 0.55
                and strength in {"strong", "moderate"}
            )
            if not actionable_status or not not_repeated:
                relevance = min(relevance, RELEVANCE_FLOOR - 0.01)
            elif scope == "single_company":
                relevance = min(relevance, 0.49)
                strength = "weak"
            if relevance < RELEVANCE_FLOOR or direction == "neutral":
                continue
            judgments.append({
                "code": code,
                "relevance": round(relevance, 3),
                "direction": direction,
                "sentiment": direction,
                "strength": strength,
                "transmission": str(
                    judgment.get("transmission") or judgment.get("reason") or ""
                )[:160],
                "direct_evidence": direct_evidence,
            })

        if not judgments:
            continue
        entities = [
            str(value).strip()[:40]
            for value in (item.get("entities") or [])[:8]
            if str(value).strip()
        ]
        event_type = str(item.get("event_type") or "other").lower()[:40]
        event_key = str(item.get("event_key") or "").strip()[:120]
        if not event_key:
            seed = "|".join(entities + [event_type, grounded_evidence[:80]])
            event_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
        parsed.append({
            "article_id": article_id,
            "title": str(candidate.get("title") or "")[:120],
            "source": str(candidate.get("source") or "")[:40],
            "event_type": event_type,
            "event_status": status,
            "novelty": novelty,
            "scope": scope,
            "event_key": event_key,
            "entities": entities,
            "evidence": evidence,
            "grounded": True,
            "etf_judgments": judgments,
        })
    return parsed


def score_news_with_llm(
    semantic_candidates: list[dict[str, Any]],
    pool_codes: list[str],
    *,
    max_retries: int = 1,
) -> dict[str, Any]:
    """Use DeepSeek to extract grounded events from high-recall candidates.

    Args:
        semantic_candidates: 规则命中与事件语言召回组成的候选列表
        pool_codes: 候选ETF代码列表
        max_retries: LLM调用失败时的重试次数

    Returns:
        包含 events 与 review_completed 的审查结果。成功审查但无事件
        与服务不可用是不同状态，前者必须保留语义否决。
    """
    if not is_available():
        print("[news_llm] DeepSeek未配置，跳过LLM新闻评分。")
        return {
            "events": [], "review_completed": False,
            "candidate_count": len(semantic_candidates), "successful_batches": 0,
            "failed_batches": 0, "reason": "llm_unavailable",
        }

    if not semantic_candidates:
        return {
            "events": [], "review_completed": False,
            "candidate_count": 0, "successful_batches": 0,
            "failed_batches": 0, "reason": "no_candidates",
        }

    if not pool_codes:
        return {
            "events": [], "review_completed": False,
            "candidate_count": len(semantic_candidates), "successful_batches": 0,
            "failed_batches": 0, "reason": "empty_etf_pool",
        }

    valid_codes = {str(c).zfill(6) for c in pool_codes}
    all_results: list[dict[str, Any]] = []
    successful_batches = 0
    failed_batches = 0
    reviewed_article_ids: list[str] = []
    failed_article_ids: list[str] = []

    # 分批处理，每批最多 MAX_ARTICLES_PER_CALL 条
    date_tag = os.environ.get("TRADE_DATE", datetime.now().strftime("%Y-%m-%d"))

    for batch_start in range(0, len(semantic_candidates), MAX_ARTICLES_PER_CALL):
        batch = semantic_candidates[batch_start:batch_start + MAX_ARTICLES_PER_CALL]
        user_prompt = _build_user_prompt(batch, sorted(valid_codes))

        for attempt in range(max_retries + 1):
            try:
                resp = call_json(
                    prompt=user_prompt,
                    system=EVENT_EXTRACTION_SYSTEM_PROMPT,
                    schema={"required": ["articles"], "types": {"articles": list}},
                    model=LLM_NEWS_MODEL,
                    temperature=LLM_NEWS_TEMPERATURE,
                    max_tokens=4096,
                    date_tag=date_tag,
                )
                raw_data = resp.get("data", {})
                # call_json + response_format=json_object 返回的data是dict
                # 我们期望 {"articles": [...]}
                if isinstance(raw_data, dict):
                    articles_data = raw_data.get("articles", [])
                    if isinstance(articles_data, list):
                        parsed = _parse_structured_response(
                            json.dumps(articles_data, ensure_ascii=False),
                            valid_codes,
                            batch,
                        )
                    else:
                        parsed = _parse_structured_response(
                            json.dumps(raw_data, ensure_ascii=False),
                            valid_codes,
                            batch,
                        )
                else:
                    parsed = _parse_structured_response(
                        str(raw_data), valid_codes, batch
                    )
                all_results.extend(parsed)
                successful_batches += 1
                reviewed_article_ids.extend(
                    str(item.get("article_id") or "") for item in batch
                )
                print(f"[news_llm] batch {batch_start}-{batch_start+len(batch)-1}: "
                      f"{len(batch)}条 → {len(parsed)}条有效评分"
                      f" (cache={resp.get('cache_hit', False)})")
                break
            except (LLMUnavailable, LLMResponseError) as e:
                print(f"[news_llm] LLM调用失败 (attempt {attempt+1}/{max_retries+1}): {e}")
                if attempt == max_retries:
                    failed_batches += 1
                    failed_article_ids.extend(
                        str(item.get("article_id") or "") for item in batch
                    )
                    print(f"[news_llm] batch {batch_start} 全部失败，跳过")
            except Exception as e:
                print(f"[news_llm] 未知错误 (attempt {attempt+1}/{max_retries+1}): {e}")
                if attempt == max_retries:
                    failed_batches += 1
                    failed_article_ids.extend(
                        str(item.get("article_id") or "") for item in batch
                    )
                    print(f"[news_llm] batch {batch_start} 全部失败，跳过")

    return {
        "events": all_results,
        "review_completed": successful_batches > 0,
        "candidate_count": len(semantic_candidates),
        "successful_batches": successful_batches,
        "failed_batches": failed_batches,
        "reviewed_article_ids": list(dict.fromkeys(reviewed_article_ids)),
        "failed_article_ids": list(dict.fromkeys(failed_article_ids)),
        "reason": "ok" if successful_batches > 0 else "all_batches_failed",
    }


def _merge_llm_into_news_signal_legacy(
    news_signal: dict[str, Any],
    llm_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """将LLM评分结果合并到新闻信号中，生成增强版主题分。

    策略：
    - 关键词评分是可复现的基础信号，LLM只做有界融合
    - LLM未提及的ETF完整保留关键词评分
    - 同时命中的ETF按 ``ETF_NEWS_LLM_WEIGHT`` 加权，默认LLM占60%
    - strength 字段：strong → 基础分0.42, moderate → 0.25, weak → 0.15
    - sentiment 字段：negative → 取负号
    """
    if not llm_results:
        return news_signal

    # 建立 title → llm_judgments 映射
    llm_by_title: dict[str, list[dict[str, Any]]] = {}
    for item in llm_results:
        key = str(item.get("title", ""))[:80].strip()
        if key:
            llm_by_title[key] = item.get("etf_judgments", [])

    # 重新计算 theme_scores
    llm_theme_scores: dict[str, float] = {}
    llm_accepted = 0
    llm_strong = 0

    for item in llm_results:
        title = str(item.get("title", ""))[:80]
        for j in item.get("etf_judgments", []):
            code = j["code"]
            relevance = j["relevance"]
            sentiment_sign = -1.0 if j["sentiment"] == "negative" else 1.0

            # strength → 基础分数
            strength = j.get("strength", "weak")
            if strength == "strong":
                base = 0.42
            elif strength == "moderate":
                base = 0.25
            else:
                base = 0.15

            score = sentiment_sign * base * max(0.5, relevance)
            score = round(max(-0.85, min(0.85, score)), 3)

            llm_theme_scores[code] = llm_theme_scores.get(code, 0.0) + score
            llm_accepted += 1
            if strength == "strong":
                llm_strong += 1

    if llm_theme_scores:
        # 裁剪到 [-0.85, 0.85]
        llm_theme_scores = {
            code: round(max(-0.85, min(0.85, v)), 3)
            for code, v in llm_theme_scores.items()
            if abs(v) >= 0.12  # WEAK_NEWS_THRESHOLD
        }

        keyword_scores = dict(
            news_signal.get("_original_theme_scores")
            or news_signal.get("theme_scores")
            or {}
        )
        try:
            llm_weight = float(os.environ.get("ETF_NEWS_LLM_WEIGHT", "0.60"))
        except (TypeError, ValueError):
            llm_weight = 0.60
        llm_weight = max(0.0, min(1.0, llm_weight))
        merged_scores: dict[str, float] = {}
        for code in sorted(set(keyword_scores) | set(llm_theme_scores)):
            has_keyword = code in keyword_scores
            has_llm = code in llm_theme_scores
            keyword_value = float(keyword_scores.get(code, 0.0) or 0.0)
            llm_value = float(llm_theme_scores.get(code, 0.0) or 0.0)
            if has_keyword and has_llm:
                value = keyword_value * (1.0 - llm_weight) + llm_value * llm_weight
            elif has_llm:
                value = llm_value
            else:
                value = keyword_value
            value = round(max(-0.85, min(0.85, value)), 3)
            if value != 0.0:
                merged_scores[code] = value

        # 文章条数/强弱仍以关键词入选为准（仓位档位读这些字段）。
        # llm_*_count 是「ETF 判断条数」，绝不能写回 accepted_count，否则 5 文×多 ETF 会灌水。
        if "_original_accepted_count" not in news_signal:
            news_signal["_original_accepted_count"] = int(
                news_signal.get("accepted_count", 0) or 0
            )
            news_signal["_original_strong_count"] = int(
                news_signal.get("strong_count", 0) or 0
            )
        news_signal["theme_scores"] = merged_scores
        news_signal["llm_theme_scores"] = llm_theme_scores
        news_signal["keyword_theme_scores_backup"] = keyword_scores
        news_signal["source"] = "keyword_llm_blend"
        news_signal["news_llm_weight"] = llm_weight
        news_signal["llm_article_count"] = len(llm_results)
        news_signal["llm_accepted_count"] = llm_accepted
        news_signal["llm_strong_count"] = llm_strong
        news_signal["max_abs_theme"] = round(
            max(abs(v) for v in merged_scores.values()) if merged_scores else 0.0,
            3,
        )
        # 仓位档位读 confidence/sentiment：与 LLM theme 对齐，避免关键词置信度压错仓位
        strong_arts = 0
        weak_arts = 0
        for item in llm_results:
            strengths = [j.get("strength") for j in (item.get("etf_judgments") or [])]
            if any(s == "strong" for s in strengths):
                strong_arts += 1
            elif strengths:
                weak_arts += 1
        news_signal["confidence"] = round(
            min(1.0, 0.20 * strong_arts + 0.04 * weak_arts), 3
        )
        market_refs = ("510300", "510050", "510500")
        market_vals = [merged_scores[c] for c in market_refs if c in merged_scores]
        news_signal["market_sentiment"] = (
            round(float(sum(market_vals) / len(market_vals)), 3) if market_vals else 0.0
        )

    # 即使 LLM 未产出有效主题，也保留原始关键词评分供审计。
    news_signal.setdefault(
        "keyword_theme_scores_backup",
        dict(news_signal.get("_original_theme_scores") or news_signal.get("theme_scores") or {}),
    )

    return news_signal


def _semantic_judgment_score(judgment: dict[str, Any]) -> float:
    base = {
        "strong": 0.42,
        "moderate": 0.25,
        "weak": 0.15,
    }.get(str(judgment.get("strength") or "weak"), 0.15)
    relevance = max(0.0, min(1.0, float(judgment.get("relevance") or 0.0)))
    direction = str(
        judgment.get("direction") or judgment.get("sentiment") or "neutral"
    )
    if direction not in {"positive", "negative"}:
        return 0.0
    score = base * max(0.5, relevance)
    if not judgment.get("direct_evidence"):
        score *= 0.5
    return round(-score if direction == "negative" else score, 3)


def merge_llm_into_news_signal(
    news_signal: dict[str, Any],
    llm_results: list[dict[str, Any]] | dict[str, Any],
) -> dict[str, Any]:
    """Merge grounded semantic events while retaining keyword audit data.

    Keyword-only mappings are deliberately downweighted after a successful
    semantic review. They remain visible for retrieval diagnostics and as a
    no-LLM fallback, but they no longer carry full conviction.
    """
    review_meta: dict[str, Any] = {}
    if isinstance(llm_results, dict):
        review_meta = dict(llm_results)
        events = list(review_meta.get("events") or [])
        review_completed = bool(review_meta.get("review_completed"))
    else:
        events = list(llm_results or [])
        review_completed = bool(events)
    if not review_completed:
        return news_signal
    llm_results = events

    candidates = list(news_signal.get("semantic_candidates") or [])
    candidate_by_id = {
        str(item.get("article_id") or ""): item
        for item in candidates
        if str(item.get("article_id") or "")
    }
    accepted = [dict(item) for item in (news_signal.get("accepted_articles") or [])]
    accepted_by_id = {
        str(item.get("article_id") or ""): index
        for index, item in enumerate(accepted)
        if str(item.get("article_id") or "")
    }
    reviewed_article_ids = {
        str(value) for value in (review_meta.get("reviewed_article_ids") or [])
        if str(value)
    }
    if reviewed_article_ids:
        for item in accepted:
            article_id = str(item.get("article_id") or "")
            item["semantic_reviewed"] = article_id in reviewed_article_ids

    event_source_sets: dict[str, set[str]] = {}
    for result in llm_results:
        event_key = _grounding_text(result.get("event_key"))
        if not event_key:
            continue
        source = str(result.get("source") or "").strip()
        if source:
            event_source_sets.setdefault(event_key, set()).add(source)

    per_code_events: dict[str, dict[str, float]] = {}
    direct_event_keys: set[str] = set()
    strong_event_keys: set[str] = set()
    moderate_event_keys: set[str] = set()
    semantic_only_added = 0

    for result in llm_results:
        article_id = str(result.get("article_id") or "")
        candidate = candidate_by_id.get(article_id, {})
        event_key = _grounding_text(result.get("event_key"))
        if not event_key:
            event_key = hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:20]
        source_count = len(event_source_sets.get(event_key, set()))
        semantic_scores: dict[str, float] = {}
        enriched_judgments: list[dict[str, Any]] = []
        for raw_judgment in result.get("etf_judgments") or []:
            judgment = dict(raw_judgment)
            score = _semantic_judgment_score(judgment)
            judgment["score"] = score
            enriched_judgments.append(judgment)
            if score == 0.0:
                continue
            code = str(judgment.get("code") or "").zfill(6)
            semantic_scores[code] = score
            code_events = per_code_events.setdefault(code, {})
            current = code_events.get(event_key)
            if current is None or abs(score) > abs(current):
                code_events[event_key] = score
            if judgment.get("direct_evidence"):
                direct_event_keys.add(event_key)
                if judgment.get("strength") == "strong":
                    strong_event_keys.add(event_key)
                else:
                    moderate_event_keys.add(event_key)

        semantic_event = {
            "event_type": result.get("event_type"),
            "event_status": result.get("event_status"),
            "novelty": result.get("novelty"),
            "scope": result.get("scope"),
            "event_key": result.get("event_key"),
            "entities": list(result.get("entities") or []),
            "evidence": result.get("evidence"),
            "grounded": bool(result.get("grounded")),
            "independent_source_count": source_count,
            "etf_judgments": enriched_judgments,
        }
        if article_id in accepted_by_id:
            index = accepted_by_id[article_id]
            accepted[index]["semantic_event"] = semantic_event
            accepted[index]["semantic_theme_scores"] = semantic_scores
            accepted[index]["semantic_reviewed"] = True
        elif any(
            judgment.get("direct_evidence") and abs(float(judgment.get("score") or 0.0)) >= 0.12
            for judgment in enriched_judgments
        ):
            quality = "strong" if any(
                judgment.get("direct_evidence") and judgment.get("strength") == "strong"
                for judgment in enriched_judgments
            ) else "weak"
            excerpt = str(candidate.get("content_excerpt") or "")
            article = {
                "accepted": True,
                "article_id": article_id,
                "title": str(candidate.get("title") or result.get("title") or ""),
                "source": str(candidate.get("source") or result.get("source") or ""),
                "url": str(candidate.get("url") or ""),
                "published_at": str(candidate.get("published_at") or ""),
                "fetched_at": str(candidate.get("fetched_at") or ""),
                "content_excerpt": excerpt,
                "content_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                "quality": quality,
                "reason": "llm_grounded_semantic_event",
                "mapping_scope": "llm_grounded_event",
                "event_hits": {
                    str(result.get("event_type") or "other"): [str(result.get("evidence") or "")[:80]]
                },
                "vague_hits": [],
                "negative_hits": ["semantic_negative"] if any(v < 0 for v in semantic_scores.values()) else [],
                "theme_scores": semantic_scores,
                "risk_flags": [],
                "semantic_event": semantic_event,
                "semantic_theme_scores": semantic_scores,
                "semantic_reviewed": True,
                "rule_accepted": False,
            }
            accepted_by_id[article_id] = len(accepted)
            accepted.append(article)
            semantic_only_added += 1

    topk_weights = (1.0, 0.5, 0.25)
    llm_theme_scores: dict[str, float] = {}
    for code, event_scores in per_code_events.items():
        strongest = sorted(event_scores.values(), key=abs, reverse=True)[:len(topk_weights)]
        value = sum(score * weight for score, weight in zip(strongest, topk_weights))
        value = round(max(-0.85, min(0.85, value)), 3)
        if abs(value) >= 0.12:
            llm_theme_scores[code] = value

    keyword_scores = dict(
        news_signal.get("_original_theme_scores")
        or news_signal.get("theme_scores")
        or {}
    )
    try:
        llm_weight = float(os.environ.get("ETF_NEWS_LLM_WEIGHT", "0.60"))
    except (TypeError, ValueError):
        llm_weight = 0.60
    try:
        keyword_only_weight = float(os.environ.get("ETF_NEWS_KEYWORD_ONLY_WEIGHT", "0.25"))
    except (TypeError, ValueError):
        keyword_only_weight = 0.25
    llm_weight = max(0.0, min(1.0, llm_weight))
    keyword_only_weight = max(0.0, min(1.0, keyword_only_weight))
    configured_keyword_only_weight = keyword_only_weight
    if int(review_meta.get("failed_batches") or 0) > 0:
        keyword_only_weight = 1.0

    merged_scores: dict[str, float] = {}
    for code in sorted(set(keyword_scores) | set(llm_theme_scores)):
        keyword_value = float(keyword_scores.get(code, 0.0) or 0.0)
        llm_value = float(llm_theme_scores.get(code, 0.0) or 0.0)
        if code in keyword_scores and code in llm_theme_scores:
            value = keyword_value * (1.0 - llm_weight) + llm_value * llm_weight
        elif code in llm_theme_scores:
            value = llm_value
        else:
            value = keyword_value * keyword_only_weight
        value = round(max(-0.85, min(0.85, value)), 3)
        if abs(value) >= 0.08:
            merged_scores[code] = value

    original_accepted_count = int(news_signal.get("accepted_count", 0) or 0)
    original_strong_count = int(news_signal.get("strong_count", 0) or 0)
    original_weak_count = int(news_signal.get("weak_count", 0) or 0)
    news_signal["_original_accepted_count"] = original_accepted_count
    news_signal["_original_strong_count"] = original_strong_count
    news_signal["accepted_articles"] = accepted
    added_strong = sum(
        1 for item in accepted[original_accepted_count:]
        if item.get("quality") == "strong"
    )
    news_signal["accepted_count"] = original_accepted_count + semantic_only_added
    news_signal["strong_count"] = original_strong_count + added_strong
    news_signal["weak_count"] = (
        original_weak_count + semantic_only_added - added_strong
    )
    news_signal["theme_scores"] = merged_scores
    news_signal["llm_theme_scores"] = llm_theme_scores
    news_signal["keyword_theme_scores_backup"] = keyword_scores
    news_signal["source"] = "grounded_semantic_event_blend"
    news_signal["news_llm_weight"] = llm_weight
    news_signal["keyword_only_weight"] = keyword_only_weight
    news_signal["semantic_review_completed"] = True
    news_signal["llm_article_count"] = len(llm_results)
    news_signal["llm_accepted_count"] = sum(
        len(item.get("etf_judgments") or []) for item in llm_results
    )
    news_signal["llm_strong_count"] = len(strong_event_keys)
    news_signal["semantic_audit"] = {
        "candidate_count": int(review_meta.get("candidate_count") or len(candidates)),
        "successful_batches": int(review_meta.get("successful_batches") or 0),
        "failed_batches": int(review_meta.get("failed_batches") or 0),
        "grounded_event_count": len(llm_results),
        "direct_event_count": len(direct_event_keys),
        "unique_event_count": len({
            _grounding_text(item.get("event_key")) for item in llm_results
        }),
        "semantic_only_articles_added": semantic_only_added,
        "keyword_only_weight": keyword_only_weight,
        "configured_keyword_only_weight": configured_keyword_only_weight,
        "event_source_counts": {
            key[:80]: len(sources) for key, sources in event_source_sets.items()
        },
    }
    news_signal["confidence"] = round(
        min(1.0, 0.20 * len(strong_event_keys) + 0.08 * len(moderate_event_keys)),
        3,
    )
    market_refs = ("510300", "510050", "510500")
    market_values = [merged_scores[code] for code in market_refs if code in merged_scores]
    news_signal["market_sentiment"] = round(
        float(sum(market_values) / len(market_values)), 3
    ) if market_values else 0.0
    news_signal["max_abs_theme"] = round(
        max((abs(value) for value in merged_scores.values()), default=0.0), 3
    )
    return news_signal
