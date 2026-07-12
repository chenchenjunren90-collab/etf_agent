"""Focused tests for ten-day goal, provenance and LLM blending controls."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from decision_snapshot import write_immutable_snapshot
from goal_state import apply_goal_overlay, summarize_goal_rows
from news_signal import score_news_article
from scoring import _inject_llm_views_into_signals


def test_goal_overlay() -> None:
    os.environ["ETF_TEN_DAY_GOAL_MODE"] = "fixed"
    achieved = summarize_goal_rows(
        "2026-07-12",
        capital=500000,
        rows=[{"date": "2026-07-11", "pnl": 2600}],
    )
    ratio, positions, audit = apply_goal_overlay(
        0.55,
        2,
        [{"volatility_20d_pct": 1.0}],
        achieved,
    )
    assert achieved["status"] == "target_achieved"
    assert ratio == 0.0 and positions == 1
    assert audit and audit["final_invest_ratio"] == 0.0

    active = summarize_goal_rows(
        "2026-07-12",
        capital=500000,
        rows=[{"date": "2026-07-11", "pnl": 0}],
    )
    ratio, _, audit = apply_goal_overlay(
        0.55,
        2,
        [{"volatility_20d_pct": 2.0}],
        active,
    )
    assert 0.15 <= ratio <= 0.16
    assert audit and audit["volatility_cap"] is not None
    os.environ.pop("ETF_TEN_DAY_GOAL_MODE", None)


def test_llm_blend() -> None:
    os.environ.pop("ETF_LLM_THEME_MODE", None)
    os.environ["ETF_LLM_THEME_MODE"] = "blend"
    os.environ["ETF_LLM_THEME_BLEND"] = "0.35"
    signals = {
        "scores": {"510300": 0.2},
        "fresh_theme_scores": {"510300": 0.2},
    }
    decision = {
        "per_etf_view": [
            {"code": "510300", "score": 0.8, "reason": "test"},
        ]
    }
    merged = _inject_llm_views_into_signals(signals, decision)
    assert abs(merged["fresh_theme_scores"]["510300"] - 0.41) < 1e-9
    hint = merged["llm_hints"]["510300"]
    assert hint["raw_score"] == 0.8
    assert hint["applied_score"] == 0.41
    os.environ.pop("ETF_LLM_THEME_MODE", None)


def test_news_provenance() -> None:
    article = {
        "title": "央行宣布降准支持沪深300",
        "content": "央行宣布降准0.5个百分点，为市场提供长期流动性，沪深300受到关注。",
        "source": "test",
        "url": "https://example.test/news/1",
        "published_at": "2026-07-12 08:30:00",
        "fetched_at": "2026-07-12 08:31:00",
    }
    scored = score_news_article(article)
    assert scored["accepted"] is True
    assert scored["published_at"] == article["published_at"]
    assert len(scored["content_sha256"]) == 64


def test_snapshot_is_immutable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        meta = write_immutable_snapshot(
            "2026-07-12",
            {"competition_output": [], "test": True},
            snapshot_dir=root,
        )
        path = root / "2026-07-12" / f"{meta['sha256']}.json"
        assert path.exists()
        before = path.read_bytes()
        write_immutable_snapshot(
            "2026-07-12",
            {"competition_output": [], "test": True},
            snapshot_dir=root,
        )
        assert path.read_bytes() == before


if __name__ == "__main__":
    test_goal_overlay()
    test_llm_blend()
    test_news_provenance()
    test_snapshot_is_immutable()
    print("PROFITABILITY CONTROLS OK")
