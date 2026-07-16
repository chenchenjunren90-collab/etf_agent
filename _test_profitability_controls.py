"""Focused tests for ten-day goal, provenance and LLM blending controls."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from backtest_provenance import sanitize_news_signal_for_backtest
from decision_snapshot import write_immutable_snapshot
from goal_state import apply_goal_overlay, build_goal_state, summarize_goal_rows
from news_signal import score_news_article
from news_time_split import split_articles_by_post_close
from news_llm_scorer import merge_llm_into_news_signal
from news_store import query_articles_before
from scoring import SHORT_RACE_POSITIVE_WEIGHT_TOTAL, _inject_llm_views_into_signals
from theme_signal import _load_signal, get_theme_signals
from strategy import _resolve_score_gate


def test_short_race_score_scale_is_normalized() -> None:
    neutral = (
        50.0 * 0.25
        + 50.0 * 0.10
        + 50.0 * 0.30
        + 50.0 * 0.20
    ) / SHORT_RACE_POSITIVE_WEIGHT_TOTAL
    assert abs(neutral - 50.0) < 1e-12


def test_profitability_probe_floor_survives_downstream_score_gate() -> None:
    assert _resolve_score_gate(50.0, None) == 50.0
    assert _resolve_score_gate(50.0, 35.0) == 35.0
    assert _resolve_score_gate(45.0, 35.0) == 35.0


def test_official_job_passes_news_into_strategy() -> None:
    source = (Path(__file__).resolve().parent / "daily_job.py").read_text(encoding="utf-8")
    assert "theme_signals_override=news_signal" in source


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

    os.environ["ETF_TEN_DAY_GOAL_MODE"] = "monitor"
    ratio, positions, audit = apply_goal_overlay(
        0.55,
        2,
        [{"volatility_20d_pct": 2.0}],
        active,
    )
    assert ratio == 0.55 and positions == 2
    assert audit and audit["volatility_cap"] is None

    os.environ["ETF_TEN_DAY_GOAL_MODE"] = "risk_cap"
    ratio, _, audit = apply_goal_overlay(
        0.55,
        2,
        [{"volatility_20d_pct": 2.0}],
        active,
    )
    assert 0.15 <= ratio <= 0.16
    assert audit and audit["volatility_cap"] is not None
    os.environ.pop("ETF_TEN_DAY_GOAL_MODE", None)


def test_goal_window_requires_explicit_start() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        state = root / "goal_window.json"
        os.environ["ETF_TEN_DAY_GOAL_MODE"] = "monitor"
        os.environ.pop("ETF_GOAL_START_DATE", None)
        monitored = build_goal_state(
            "2026-07-12",
            capital=500000,
            output_dir=root / "outputs",
            data_dir=root,
            state_path=state,
        )
        assert monitored["enabled"] is True
        assert monitored["window_mode"] == "rolling_monitor"
        assert not state.exists()

        os.environ["ETF_TEN_DAY_GOAL_MODE"] = "fixed"
        fixed = build_goal_state(
            "2026-07-12",
            capital=500000,
            output_dir=root / "outputs",
            data_dir=root,
            state_path=state,
        )
        assert fixed["enabled"] is False
        assert fixed["status"] == "configuration_required"
        assert not state.exists()
        os.environ.pop("ETF_TEN_DAY_GOAL_MODE", None)


def test_llm_blend() -> None:
    os.environ["ETF_LLM_THEME_MODE"] = "blend"
    os.environ["ETF_LLM_THEME_BLEND"] = "0.35"
    os.environ.pop("ETF_ALLOW_LLM_SCORE_CONTROL", None)
    signals = {
        "scores": {"510300": 0.2},
        "fresh_theme_scores": {"510300": 0.2},
    }
    decision = {
        "per_etf_view": [
            {"code": "510300", "score": 0.8, "reason": "test"},
        ]
    }
    audited = _inject_llm_views_into_signals(signals, decision)
    assert audited["fresh_theme_scores"]["510300"] == 0.2
    assert audited["llm_score_control_enabled"] is False
    assert audited["llm_hints"]["510300"]["mode"] == "audit"

    os.environ["ETF_ALLOW_LLM_SCORE_CONTROL"] = "1"
    merged = _inject_llm_views_into_signals(signals, decision)
    assert abs(merged["fresh_theme_scores"]["510300"] - 0.41) < 1e-9
    hint = merged["llm_hints"]["510300"]
    assert hint["raw_score"] == 0.8
    assert hint["applied_score"] == 0.41
    os.environ.pop("ETF_LLM_THEME_MODE", None)
    os.environ.pop("ETF_LLM_THEME_BLEND", None)
    os.environ.pop("ETF_ALLOW_LLM_SCORE_CONTROL", None)


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


def test_news_body_does_not_contaminate_unrelated_etf() -> None:
    article = {
        "title": "半导体公司并购重组获批",
        "summary": "芯片产业链整合取得实质性进展。",
        "content": "公告聚焦半导体并购。文末市场综述还提到医药和消费行业。",
        "source": "test",
        "published_at": "2026-07-12 08:30:00",
    }
    scored = score_news_article(article)
    assert scored["accepted"] is True
    assert scored["mapping_scope"] == "core_event_fields"
    assert "588000" in scored["theme_scores"]
    assert "512010" not in scored["theme_scores"]


def test_news_backtest_provenance_is_point_in_time() -> None:
    base = {
        "quality": "strong",
        "event_hits": {"policy": ["test"]},
        "theme_scores": {"510300": 0.4},
    }
    signal = {
        "cutoff_time": "09:30",
        "accepted_articles": [
            {**base, "title": "valid", "published_at": "2026-07-13 08:30:00"},
            {**base, "title": "future", "published_at": "2026-07-13 10:00:00"},
            {**base, "title": "missing", "published_at": ""},
            {
                **base,
                "title": "fetched late",
                "published_at": "2026-07-13 08:20:00",
                "fetched_at": "2026-07-13 09:31:00",
            },
        ],
    }
    cleaned = sanitize_news_signal_for_backtest(signal, "2026-07-13")
    assert [item["title"] for item in cleaned["fresh_accepted_articles"]] == ["valid"]
    assert cleaned["fresh_theme_scores"] == {"510300": 0.4}
    audit = cleaned["backtest_provenance"]
    assert audit["input_articles"] == 4
    assert audit["accepted_articles"] == 1
    assert audit["rejected_articles"] == 3


def test_old_news_archive_recovers_timestamp_from_raw_article() -> None:
    accepted = {
        "title": "央行降准支持市场",
        "source": "test",
        "url": "https://example.test/old/1",
        "quality": "strong",
        "event_hits": {"policy": ["降准"]},
        "theme_scores": {"510300": 0.4},
    }
    signal = {
        "cutoff_time": "09:30",
        "accepted_articles": [accepted],
        "raw_articles": [{
            "title": accepted["title"],
            "source": accepted["source"],
            "url": accepted["url"],
            "published_at": "2026-07-13 08:30:00",
        }],
    }
    cleaned = sanitize_news_signal_for_backtest(signal, "2026-07-13")
    assert cleaned["accepted_count"] == 1
    assert cleaned["fresh_accepted_articles"][0]["published_at"] == "2026-07-13 08:30:00"


def test_old_news_archive_rejects_raw_article_published_after_cutoff() -> None:
    signal = {
        "cutoff_time": "09:30",
        "accepted_articles": [{
            "title": "late",
            "source": "test",
            "url": "https://example.test/old/late",
            "quality": "strong",
            "theme_scores": {"510300": 0.4},
        }],
        "raw_articles": [{
            "title": "late",
            "source": "test",
            "url": "https://example.test/old/late",
            "published_at": "2026-07-13 09:31:00",
        }],
    }
    cleaned = sanitize_news_signal_for_backtest(signal, "2026-07-13")
    assert cleaned["accepted_count"] == 0
    assert cleaned["backtest_provenance"]["rejection_reasons"] == {
        "published_after_decision_cutoff": 1,
    }


def test_missing_news_database_is_not_created_by_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "missing.db"
        assert query_articles_before("2026-07-13", db_path=db_path) == []
        assert not db_path.exists()


def test_missing_timestamp_never_enters_fresh_news() -> None:
    fresh, stale, _ = split_articles_by_post_close(
        [{"title": "unknown", "published_at": ""}],
        "2026-07-13",
    )
    assert fresh == []
    assert len(stale) == 1


def test_theme_signal_preserves_direct_article_evidence() -> None:
    raw = {
        "fresh_theme_scores": {"510300": 0.4},
        "fresh_accepted_articles": [{"title": "direct", "theme_scores": {"510300": 0.4}}],
        "stale_accepted_articles": [],
        "accepted_articles": [{"title": "direct", "theme_scores": {"510300": 0.4}}],
        "fresh_accepted_count": 1,
    }
    with patch("theme_signal._load_signal", return_value=raw):
        loaded = get_theme_signals("2026-07-13")
    assert loaded["fresh_accepted_articles"][0]["title"] == "direct"


def test_missing_historical_news_never_uses_latest_auto_signal() -> None:
    with patch("theme_signal.signal_path") as path, patch("theme_signal.AUTO_SIGNAL_PATH") as auto:
        path.return_value.exists.return_value = False
        auto.exists.return_value = True
        assert _load_signal("2020-01-01") == {}


def test_news_llm_preserves_keyword_only_etfs() -> None:
    signal = {
        "theme_scores": {"510300": 0.4, "510500": -0.2},
        "_original_theme_scores": {"510300": 0.4, "510500": -0.2},
        "accepted_count": 1,
        "strong_count": 1,
        "accepted_articles": [{"title": "test"}],
    }
    merged = merge_llm_into_news_signal(
        signal,
        [
            {
                "title": "test",
                "etf_judgments": [
                    {
                        "code": "510300",
                        "relevance": 1.0,
                        "sentiment": "positive",
                        "strength": "strong",
                    }
                ],
            }
        ],
    )
    assert merged["theme_scores"]["510500"] == -0.2
    assert merged["theme_scores"]["510300"] == 0.412
    assert merged["keyword_theme_scores_backup"]["510500"] == -0.2


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
    test_short_race_score_scale_is_normalized()
    test_profitability_probe_floor_survives_downstream_score_gate()
    test_official_job_passes_news_into_strategy()
    test_goal_overlay()
    test_goal_window_requires_explicit_start()
    test_llm_blend()
    test_news_provenance()
    test_news_body_does_not_contaminate_unrelated_etf()
    test_news_backtest_provenance_is_point_in_time()
    test_old_news_archive_recovers_timestamp_from_raw_article()
    test_old_news_archive_rejects_raw_article_published_after_cutoff()
    test_missing_news_database_is_not_created_by_read()
    test_missing_timestamp_never_enters_fresh_news()
    test_theme_signal_preserves_direct_article_evidence()
    test_missing_historical_news_never_uses_latest_auto_signal()
    test_news_llm_preserves_keyword_only_etfs()
    test_snapshot_is_immutable()
    print("PROFITABILITY CONTROLS OK")
