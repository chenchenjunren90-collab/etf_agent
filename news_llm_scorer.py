"""News semantic scoring via DeepSeek — replaces pure keyword matching.

Two-stage architecture:
  Stage 1 (fast): keyword pre-filter in news_signal.py → removes obvious noise
  Stage 2 (precise): this module → batch-send pre-filtered articles to DeepSeek
    for semantic relevance, sentiment, and ETF mapping judgment.

Anti-hallucination measures:
  - Prompt constrains output to only ETF codes in the candidate pool
  - Requires structured JSON with reason for each judgment
  - Relevance < 0.3 is auto-discarded regardless of sentiment
  - Single article capped at max 3 ETFs to prevent over-propagation
  - Fallback: if LLM fails, falls back to keyword scores (graceful degradation)
"""

from __future__ import annotations

import json
import os
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
SYSTEM_PROMPT = """你是专业金融新闻语义分析系统。你的输出将被量化策略直接使用。

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
        title = str(art.get("title", ""))[:120]
        content = str(art.get("content", ""))[:300]
        source = str(art.get("source", ""))[:32]
        lines.append(f"[{idx}] 来源:{source} | {title}")
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


def score_news_with_llm(
    pre_filtered_articles: list[dict[str, Any]],
    pool_codes: list[str],
    *,
    max_retries: int = 1,
) -> list[dict[str, Any]]:
    """用DeepSeek对预筛选后的新闻做语义评分。

    Args:
        pre_filtered_articles: 经过关键词预筛选后的新闻列表
        pool_codes: 候选ETF代码列表
        max_retries: LLM调用失败时的重试次数

    Returns:
        LLM评分结果列表，每项包含 title 和 etf_judgments。
        失败时返回空列表（调用方应降级到关键词评分）。
    """
    if not is_available():
        print("[news_llm] DeepSeek未配置，跳过LLM新闻评分。")
        return []

    if not pre_filtered_articles:
        return []

    if not pool_codes:
        return []

    valid_codes = {str(c).zfill(6) for c in pool_codes}
    all_results: list[dict[str, Any]] = []

    # 分批处理，每批最多 MAX_ARTICLES_PER_CALL 条
    date_tag = os.environ.get("TRADE_DATE", datetime.now().strftime("%Y-%m-%d"))

    for batch_start in range(0, len(pre_filtered_articles), MAX_ARTICLES_PER_CALL):
        batch = pre_filtered_articles[batch_start:batch_start + MAX_ARTICLES_PER_CALL]
        user_prompt = _build_user_prompt(batch, sorted(valid_codes))

        for attempt in range(max_retries + 1):
            try:
                resp = call_json(
                    prompt=user_prompt,
                    system=SYSTEM_PROMPT,
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
                        parsed = _parse_llm_response(
                            json.dumps(articles_data, ensure_ascii=False),
                            valid_codes,
                        )
                    else:
                        parsed = _parse_llm_response(
                            json.dumps(raw_data, ensure_ascii=False),
                            valid_codes,
                        )
                else:
                    parsed = _parse_llm_response(str(raw_data), valid_codes)
                all_results.extend(parsed)
                print(f"[news_llm] batch {batch_start}-{batch_start+len(batch)-1}: "
                      f"{len(batch)}条 → {len(parsed)}条有效评分"
                      f" (cache={resp.get('cache_hit', False)})")
                break
            except (LLMUnavailable, LLMResponseError) as e:
                print(f"[news_llm] LLM调用失败 (attempt {attempt+1}/{max_retries+1}): {e}")
                if attempt == max_retries:
                    print(f"[news_llm] batch {batch_start} 全部失败，跳过")
            except Exception as e:
                print(f"[news_llm] 未知错误 (attempt {attempt+1}/{max_retries+1}): {e}")
                if attempt == max_retries:
                    print(f"[news_llm] batch {batch_start} 全部失败，跳过")

    return all_results


def merge_llm_into_news_signal(
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
