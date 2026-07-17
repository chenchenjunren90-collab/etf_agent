"""Focused invariants for grounded semantic news events."""

from __future__ import annotations

import json

from news_llm_scorer import _parse_structured_response, merge_llm_into_news_signal
from news_signal import build_news_signal
from profitability_evidence import _direct_news_support


def _candidate(article_id: str = "a1") -> dict:
    return {
        "article_id": article_id,
        "title": "监管部门宣布行业新规正式实施",
        "source": "official",
        "content_excerpt": "监管部门宣布行业新规正式实施，自今日起执行。",
        "published_at": "2026-07-17 07:00:00",
        "rule_accepted": False,
    }


def _event(article_id: str = "a1", *, scope: str = "sector") -> dict:
    return {
        "article_id": article_id,
        "event_type": "regulation",
        "event_status": "occurred",
        "novelty": "new",
        "scope": scope,
        "event_key": "监管部门|regulation|新规实施",
        "entities": ["监管部门"],
        "evidence": "行业新规正式实施",
        "etf_judgments": [{
            "code": "512880",
            "relevance": 0.85,
            "direction": "positive",
            "strength": "strong",
            "transmission": "行业制度变化影响券商业务",
        }],
    }


def test_high_recall_candidates_include_rule_misses_with_body() -> None:
    signal = build_news_signal([{
        "title": "有关部门宣布建立低空经济专项支持机制",
        "content": "首批项目计划投入100亿元，相关措施自今日起实施。",
        "source": "official",
        "published_at": "2026-07-17 07:00:00",
    }])
    assert signal["accepted_count"] == 0
    assert signal["semantic_candidate_count"] == 1
    candidate = signal["semantic_candidates"][0]
    assert candidate["rule_accepted"] is False
    assert "100亿元" in candidate["content_excerpt"]
    assert candidate["article_id"]


def test_llm_evidence_must_be_grounded_in_article() -> None:
    candidate = _candidate()
    grounded = _parse_structured_response(
        json.dumps([_event()], ensure_ascii=False),
        {"512880"},
        [candidate],
    )
    assert len(grounded) == 1
    assert grounded[0]["grounded"] is True
    assert grounded[0]["etf_judgments"][0]["direct_evidence"] is True

    hallucinated = _event()
    hallucinated["evidence"] = "原文中不存在的盈利翻倍"
    assert _parse_structured_response(
        json.dumps([hallucinated], ensure_ascii=False),
        {"512880"},
        [candidate],
    ) == []


def test_single_company_event_is_not_direct_etf_evidence() -> None:
    parsed = _parse_structured_response(
        json.dumps([_event(scope="single_company")], ensure_ascii=False),
        {"512880"},
        [_candidate()],
    )
    judgment = parsed[0]["etf_judgments"][0]
    assert judgment["direct_evidence"] is False
    assert judgment["strength"] == "weak"


def test_company_product_and_broker_forecast_are_not_direct_etf_evidence() -> None:
    company_candidate = {
        "article_id": "company",
        "title": "华为推出星河AI数据中心网络方案",
        "source": "wire",
        "content_excerpt": "华为推出星河AI数据中心网络方案，Token生产效率提升2至5倍。",
    }
    company_event = {
        "article_id": "company",
        "event_type": "technology",
        "event_status": "announced",
        "novelty": "new",
        "scope": "sector",
        "event_key": "华为|产品|AI数据中心网络方案",
        "entities": ["华为"],
        "evidence": "华为推出星河AI数据中心网络方案",
        "etf_judgments": [{
            "code": "588000",
            "relevance": 0.7,
            "direction": "positive",
            "strength": "strong",
            "transmission": "利好科技ETF，科创50包含相关产业链公司",
        }],
    }
    parsed = _parse_structured_response(
        json.dumps([company_event], ensure_ascii=False),
        {"588000"},
        [company_candidate],
    )
    judgment = parsed[0]["etf_judgments"][0]
    assert judgment["direct_evidence"] is False
    assert judgment["direct_evidence_reason"] in {
        "sector_scope_not_supported_by_source", "indirect_value_chain_claim"
    }

    broker_candidate = {
        "article_id": "broker",
        "title": "广发证券：液冷交换机、液冷光模块迎来大量新增需求",
        "source": "wire",
        "content_excerpt": "广发证券研报预计液冷交换机需求增长。",
    }
    broker_event = dict(company_event)
    broker_event.update({
        "article_id": "broker",
        "event_key": "广发证券|研报|液冷需求",
        "entities": ["广发证券"],
        "evidence": "广发证券研报预计液冷交换机需求增长",
    })
    parsed = _parse_structured_response(
        json.dumps([broker_event], ensure_ascii=False),
        {"588000"},
        [broker_candidate],
    )
    judgment = parsed[0]["etf_judgments"][0]
    assert judgment["direct_evidence"] is False
    assert judgment["direct_evidence_reason"] == "forecast_or_broker_opinion"


def test_sector_wide_realized_event_can_remain_direct() -> None:
    candidate = {
        "article_id": "sector",
        "title": "功率半导体行业进入涨价周期",
        "source": "wire",
        "content_excerpt": "功率半导体行业进入涨价周期，多家龙头企业密集宣布涨价。",
    }
    event = {
        "article_id": "sector",
        "event_type": "supply",
        "event_status": "occurred",
        "novelty": "new",
        "scope": "sector",
        "event_key": "功率半导体|涨价|行业周期",
        "entities": ["功率半导体行业"],
        "evidence": "功率半导体行业进入涨价周期",
        "etf_judgments": [{
            "code": "588000",
            "relevance": 0.7,
            "direction": "positive",
            "strength": "moderate",
            "transmission": "半导体行业价格上调改善行业收入",
        }],
    }
    parsed = _parse_structured_response(
        json.dumps([event], ensure_ascii=False), {"588000"}, [candidate]
    )
    judgment = parsed[0]["etf_judgments"][0]
    assert judgment["direct_evidence"] is True
    assert judgment["direct_evidence_reason"] == "sector_wide_grounded_event"


def test_semantic_event_can_recover_rule_miss_but_keyword_cannot_self_confirm() -> None:
    signal = {
        "theme_scores": {"510300": 0.4},
        "_original_theme_scores": {"510300": 0.4},
        "accepted_count": 1,
        "strong_count": 1,
        "accepted_articles": [{
            "article_id": "keyword",
            "title": "A股市场观点",
            "source": "commentary",
            "quality": "strong",
            "theme_scores": {"510300": 0.4},
        }],
        "semantic_candidates": [_candidate()],
    }
    parsed = _parse_structured_response(
        json.dumps([_event()], ensure_ascii=False),
        {"510300", "512880"},
        [_candidate()],
    )
    merged = merge_llm_into_news_signal(signal, parsed)
    assert merged["semantic_review_completed"] is True
    assert merged["theme_scores"]["510300"] == 0.1
    assert any(
        article.get("mapping_scope") == "llm_grounded_event"
        for article in merged["accepted_articles"]
    )
    keyword_support = _direct_news_support("510300", merged)
    semantic_support = _direct_news_support("512880", merged)
    assert keyword_support["semantic_confirmed_count"] == 0
    assert keyword_support["semantic_unconfirmed_count"] == 1
    assert semantic_support["semantic_confirmed_count"] == 1
    assert semantic_support["strong_count"] == 1


def test_successful_empty_review_is_not_treated_as_llm_failure() -> None:
    signal = {
        "theme_scores": {"510300": 0.4},
        "accepted_count": 1,
        "strong_count": 1,
        "weak_count": 0,
        "accepted_articles": [{
            "article_id": "a1",
            "title": "只有市场观点，没有可核验事件",
            "quality": "strong",
            "theme_scores": {"510300": 0.4},
        }],
        "semantic_candidates": [_candidate()],
    }
    merged = merge_llm_into_news_signal(signal, {
        "events": [],
        "review_completed": True,
        "candidate_count": 1,
        "successful_batches": 1,
        "failed_batches": 0,
    })
    assert merged["semantic_review_completed"] is True
    assert merged["theme_scores"]["510300"] == 0.1
    assert merged["confidence"] == 0.0
    support = _direct_news_support("510300", merged)
    assert support["semantic_confirmed_count"] == 0
    assert support["semantic_unconfirmed_count"] == 1


def test_partial_llm_failure_falls_back_only_for_failed_articles() -> None:
    articles = [
        {
            "article_id": "reviewed",
            "title": "沪深300政策落地",
            "quality": "strong",
            "theme_scores": {"510300": 0.4},
        },
        {
            "article_id": "failed",
            "title": "沪深300改革政策落地",
            "quality": "strong",
            "theme_scores": {"510300": 0.4},
        },
    ]
    signal = {
        "theme_scores": {"510300": 0.4},
        "accepted_count": 2,
        "strong_count": 2,
        "weak_count": 0,
        "accepted_articles": articles,
        "semantic_candidates": articles,
    }
    merged = merge_llm_into_news_signal(signal, {
        "events": [],
        "review_completed": True,
        "candidate_count": 2,
        "successful_batches": 1,
        "failed_batches": 1,
        "reviewed_article_ids": ["reviewed"],
        "failed_article_ids": ["failed"],
    })
    assert merged["keyword_only_weight"] == 1.0
    support = _direct_news_support("510300", merged)
    assert support["semantic_unconfirmed_count"] == 1
    assert support["strong_count"] == 1


if __name__ == "__main__":
    test_high_recall_candidates_include_rule_misses_with_body()
    test_llm_evidence_must_be_grounded_in_article()
    test_single_company_event_is_not_direct_etf_evidence()
    test_company_product_and_broker_forecast_are_not_direct_etf_evidence()
    test_sector_wide_realized_event_can_remain_direct()
    test_semantic_event_can_recover_rule_miss_but_keyword_cannot_self_confirm()
    test_successful_empty_review_is_not_treated_as_llm_failure()
    test_partial_llm_failure_falls_back_only_for_failed_articles()
    print("SEMANTIC NEWS TESTS OK")
