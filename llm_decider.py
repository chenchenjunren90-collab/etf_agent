"""LLM-driven daily decision fuser (fresh news first in prompt, econ second).

Builds a single prompt from post-close fresh news, economic calendar, supplement
news, and ETF price features; calls DeepSeek; returns a trace dict for
``strategy.py`` and the audit log.  On failure, callers get ``None`` and fall
back to the rule-only path.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from llm_client import (
    DEFAULT_MODEL,
    LLMResponseError,
    LLMUnavailable,
    call_json,
)


BASE_DIR = Path(__file__).resolve().parent
PROMPT_PATH = BASE_DIR / "prompts" / "decider_zh.md"
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"


# 输出 JSON 的最小校验 schema —— 字段必须齐，类型必须对。
DECIDER_SCHEMA = {
    "required": [
        "regime",
        "regime_reason",
        "econ_drivers",
        "news_drivers",
        "per_etf_view",
        "cash_decision",
        "position_ratio_hint",
        "summary_zh",
    ],
    "types": {
        "regime": str,
        "regime_reason": str,
        "econ_drivers": list,
        "news_drivers": list,
        "per_etf_view": list,
        "cash_decision": str,
        "position_ratio_hint": (int, float),
        "summary_zh": str,
    },
}

VALID_REGIME = {"risk_off", "neutral", "risk_on"}
VALID_CASH = {"stay_cash", "partial", "full_invest"}

WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _load_prompt_template() -> str:
    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"prompt template missing: {PROMPT_PATH}")
    return PROMPT_PATH.read_text(encoding="utf-8")


def _format_pool_features(
    pool: list[dict[str, Any]],
    features_by_code: dict[str, dict[str, Any]],
) -> str:
    """Render the pool + feature snapshot as a fixed-width table."""
    header = (
        "| code | name | category | ret_1d | ret_3d | ret_5d | "
        "rsi | volume_ratio | trend_score |"
    )
    sep = "|------|------|----------|--------|--------|--------|------|--------------|-------------|"
    lines = [header, sep]
    for item in pool:
        code = str(item.get("code") or "").zfill(6)
        f = features_by_code.get(code) or {}
        name = item.get("name") or ""
        cat = item.get("category") or ""
        lines.append(
            "| {code} | {name} | {cat} | {r1:+.2f}% | {r3:+.2f}% | {r5:+.2f}% | "
            "{rsi:.1f} | {vr:.2f} | {ts:.1f} |".format(
                code=code,
                name=name,
                cat=cat,
                r1=float(f.get("ret_1d") or 0.0),
                r3=float(f.get("ret_3d") or 0.0),
                r5=float(f.get("ret_5d") or 0.0),
                rsi=float(f.get("rsi") or 50.0),
                vr=float(f.get("volume_ratio") or 1.0),
                ts=float(f.get("trend_score") or 50.0),
            )
        )
    return "\n".join(lines)


def _format_fresh_news_scores(fresh_scores: dict) -> str:
    """格式化新鲜新闻主题分汇总表，供决策大模型参考。"""
    if not fresh_scores:
        return "（昨日收盘后无新鲜新闻）"
    lines = ["| ETF代码 | 新鲜主题分 | 信号方向 |"]
    lines.append("|---------|-----------|----------|")
    for code, score in sorted(fresh_scores.items(), key=lambda x: -abs(float(x[1])))[:15]:
        direction = "利好" if float(score) > 0.15 else ("利空" if float(score) < -0.15 else "中性")
        lines.append(f"| {code} | {float(score):+.3f} | {direction} |")
    return "\n".join(lines)


def build_prompt(
    *,
    date_str: str,
    capital: float,
    pool: list[dict[str, Any]],
    features_by_code: dict[str, dict[str, Any]],
    econ_text: str,
    news_text: str,
    fresh_news_text: str = "",
    fresh_news_scores_text: str = "",
) -> str:
    template = _load_prompt_template()
    weekday = datetime.strptime(date_str[:10], "%Y-%m-%d").weekday()
    return (
        template
        .replace("{{DATE}}", date_str[:10])
        .replace("{{TODAY_DOW}}", WEEKDAY_ZH[weekday])
        .replace("{{CAPITAL}}", f"{capital:,.0f}")
        .replace("{{FRESH_NEWS_SCORES}}", fresh_news_scores_text or "（无）")
        .replace("{{FRESH_NEWS}}", fresh_news_text or "（无）")
        .replace("{{ECON}}", econ_text or "（无经济日历事件）")
        .replace("{{NEWS}}", news_text or "（无入选新闻）")
        .replace("{{POOL_FEATURES}}", _format_pool_features(pool, features_by_code))
    )


def _clean_per_etf_view(
    raw_views: list[Any],
    pool_codes: set[str],
) -> list[dict[str, Any]]:
    """Drop entries pointing outside the pool, de-dup, clip score range."""
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for entry in raw_views or []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code") or "").zfill(6)
        if not code or code in seen or code not in pool_codes:
            continue
        seen.add(code)
        try:
            score = float(entry.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        score = max(-1.0, min(1.0, score))
        cleaned.append({
            "code": code,
            "score": round(score, 3),
            "reason": str(entry.get("reason") or "")[:160],
        })
    return cleaned


def _normalise_decision(
    raw: dict[str, Any],
    *,
    pool_codes: set[str],
) -> dict[str, Any]:
    regime = str(raw.get("regime") or "neutral").strip().lower()
    if regime not in VALID_REGIME:
        regime = "neutral"

    cash = str(raw.get("cash_decision") or "partial").strip().lower()
    if cash not in VALID_CASH:
        cash = "partial"

    try:
        ratio = float(raw.get("position_ratio_hint") or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    ratio = max(0.0, min(1.0, ratio))
    if cash == "stay_cash":
        ratio = 0.0
    elif cash == "full_invest" and ratio < 0.5:
        ratio = max(ratio, 0.7)

    return {
        "regime": regime,
        "regime_reason": str(raw.get("regime_reason") or "")[:300],
        "econ_drivers": list(raw.get("econ_drivers") or [])[:10],
        "news_drivers": list(raw.get("news_drivers") or [])[:10],
        "per_etf_view": _clean_per_etf_view(raw.get("per_etf_view"), pool_codes),
        "cash_decision": cash,
        "position_ratio_hint": round(float(ratio), 3),
        "summary_zh": str(raw.get("summary_zh") or "")[:600],
    }


def _save_debug(date_str: str, prompt: str, response_payload: dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = OUTPUT_DIR / f"{date_str[:10]}_llm_prompt.txt"
    resp_path = OUTPUT_DIR / f"{date_str[:10]}_llm_response.json"
    prompt_path.write_text(prompt, encoding="utf-8")
    resp_path.write_text(
        json.dumps(response_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def decide(
    *,
    date_str: str,
    capital: float,
    pool: list[dict[str, Any]],
    pool_features: dict[str, dict[str, Any]],
    econ_payload: dict[str, Any],
    news_signal: dict[str, Any],
    news_summary: list[dict[str, Any]] | None = None,
    use_cache: bool = True,
    cache_only: bool = False,
    model: str = DEFAULT_MODEL,
    date_tag: str | None = None,
    save_debug: bool = True,
) -> dict[str, Any] | None:
    """Run the LLM decision.  Returns ``None`` if the LLM is unavailable.

    The returned dict has the following shape::

        {
            "decision": {regime, per_etf_view, cash_decision, ...},
            "prompt_hash": "...",
            "model": "...",
            "usage": {prompt_tokens, completion_tokens, total_tokens},
            "cache_hit": bool,
            "econ_payload_snapshot": {...},
            "news_summary_snapshot": [...],
        }
    """
    from econ_calendar import render_for_prompt as render_econ
    from news_signal import render_news_for_prompt, summarize_for_llm

    # ── 分层新闻：新鲜 vs 陈旧（{{NEWS}} 只用陈旧，避免与 FRESH_NEWS 重复）──
    fresh_scores = (news_signal or {}).get("fresh_theme_scores", {})
    fresh_articles = (news_signal or {}).get("fresh_accepted_articles", [])
    stale_articles = (news_signal or {}).get("stale_accepted_articles", [])
    fresh_news_text = render_news_for_prompt(
        summarize_for_llm({"accepted_articles": fresh_articles} if fresh_articles else {})
    ) if fresh_articles else ""
    fresh_scores_text = _format_fresh_news_scores(fresh_scores)

    summary = news_summary
    if summary is None:
        summary = summarize_for_llm(
            {"accepted_articles": stale_articles}
            if stale_articles or "stale_accepted_articles" in (news_signal or {})
            else (news_signal or {})
        )
    econ_text = render_econ(econ_payload or {})
    news_text = render_news_for_prompt(summary)

    prompt = build_prompt(
        date_str=date_str,
        capital=capital,
        pool=pool,
        features_by_code=pool_features,
        econ_text=econ_text,
        news_text=news_text,
        fresh_news_text=fresh_news_text,
        fresh_news_scores_text=fresh_scores_text,
    )

    try:
        raw = call_json(
            prompt,
            schema=DECIDER_SCHEMA,
            model=model,
            temperature=0.2,
            max_tokens=3500,
            date_tag=date_tag or date_str[:10],
            use_cache=use_cache,
            cache_only=cache_only,
            retries=3,
        )
    except (LLMUnavailable, LLMResponseError) as exc:
        print(f"[llm_decider] {date_str} unavailable: {exc}")
        if save_debug:
            try:
                _save_debug(date_str, prompt, {"error": str(exc)})
            except Exception:
                pass
        return None

    pool_codes = {str(item.get("code") or "").zfill(6) for item in pool}
    decision = _normalise_decision(raw["data"], pool_codes=pool_codes)

    payload = {
        "decision": decision,
        "prompt_hash": raw.get("prompt_hash"),
        "model": raw.get("model"),
        "usage": raw.get("usage", {}),
        "cache_hit": bool(raw.get("cache_hit")),
        "cached_at": raw.get("cached_at"),
        "econ_payload_snapshot": {
            "date": econ_payload.get("date"),
            "source": econ_payload.get("source"),
            "high_importance_events": econ_payload.get("high_importance_events", []),
            "medium_importance_events": econ_payload.get("medium_importance_events", []),
            "has_high_impact_event": bool(econ_payload.get("has_high_impact_event")),
        },
        "news_summary_snapshot": summary,
    }

    if save_debug:
        try:
            _save_debug(date_str, prompt, {**payload, "raw_response": raw["data"]})
        except Exception as exc:
            print(f"[llm_decider] failed to write debug files: {exc}")

    return payload


__all__ = ["decide", "build_prompt", "DECIDER_SCHEMA"]
