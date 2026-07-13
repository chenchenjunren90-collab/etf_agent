"""Focused tests for the high-confidence profitability evidence gate."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

import profitability_evidence as evidence
import features
from features import _calc_short_race_features


def _candidate(**overrides):
    candidate = {
        "code": "510300",
        "name": "沪深300ETF",
        "score": 72.0,
        "fresh_theme_raw": 0.0,
        "price_position_20d": 0.65,
        "ret_10d": 2.0,
        "rsi": 58.0,
        "high_break": False,
    }
    candidate.update(overrides)
    return candidate


def _empirical(probability=0.60, expected_net=0.0010, lower_net=0.0002):
    return {
        "available": True,
        "sample_count": 100,
        "neighbor_count": 36,
        "positive_probability": probability,
        "expected_net_return": expected_net,
        "lower_expected_net": lower_net,
    }


def test_gate_actions() -> None:
    with patch.object(evidence, "estimate_empirical_edge", return_value=_empirical()):
        strong = evidence.evaluate_candidate(_candidate(), {}, "2026-07-13")
        assert strong["action"] == "trade"
        assert strong["exposure_cap"] == 0.20

    with patch.object(
        evidence,
        "estimate_empirical_edge",
        return_value=_empirical(probability=0.54, expected_net=0.0004, lower_net=-0.0001),
    ):
        cautious = evidence.evaluate_candidate(_candidate(), {}, "2026-07-13")
        assert cautious["action"] == "conservative"
        assert cautious["exposure_cap"] == 0.08

    with patch.object(
        evidence,
        "estimate_empirical_edge",
        return_value=_empirical(probability=0.49, expected_net=-0.0002, lower_net=-0.001),
    ):
        rejected = evidence.evaluate_candidate(_candidate(), {}, "2026-07-13")
        assert rejected["action"] == "cash"


def test_news_and_entry_risk_can_veto() -> None:
    with patch.object(evidence, "estimate_empirical_edge", return_value=_empirical()):
        unsupported = evidence.evaluate_candidate(
            _candidate(fresh_theme_raw=0.4),
            {"confidence": 0.8, "fresh_accepted_articles": []},
            "2026-07-13",
        )
        assert unsupported["action"] == "cash"
        assert unsupported["reason"] == "news_event_not_economically_verified"

        stretched = evidence.evaluate_candidate(
            _candidate(price_position_20d=0.93, ret_10d=9.0, rsi=75.0),
            {},
            "2026-07-13",
        )
        assert stretched["action"] == "cash"
        assert stretched["reason"] == "overextended_without_breakout"


def test_estimator_uses_only_strictly_prior_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dates = pd.bdate_range("2025-10-01", periods=120)
        returns = 0.0004 + 0.003 * np.sin(np.arange(len(dates)) / 4.0)
        close = 4.0 * np.cumprod(1.0 + returns)
        frame = pd.DataFrame({
            "date": dates.strftime("%Y-%m-%d"),
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": 1_000_000 + (np.arange(len(dates)) % 10) * 10_000,
        })
        frame.to_csv(root / "510300.csv", index=False)
        target_index = 100
        target_date = frame.iloc[target_index]["date"]
        current = _calc_short_race_features(frame.iloc[:target_index].tail(120).reset_index(drop=True))
        evidence._SAMPLE_CACHE.clear()
        result = evidence.estimate_empirical_edge(
            "510300",
            current,
            target_date,
            data_dir=root,
        )
        assert result["available"] is True
        assert result["sample_count"] >= evidence.MIN_HISTORY_SAMPLES
        assert result["latest_sample_date"] < target_date


def test_offline_price_read_never_refreshes_network() -> None:
    frame = pd.DataFrame({
        "date": pd.bdate_range("2026-01-01", periods=30),
        "close": np.linspace(4.0, 4.2, 30),
        "volume": np.full(30, 1_000_000),
    })
    with patch.dict(
        "os.environ",
        {"ETF_AGENT_STRICT_DATA": "1", "ETF_AGENT_ALLOW_NETWORK": "0"},
    ), patch.object(features, "_load_local_price", return_value=frame), patch(
        "market_data.load_fresh_price",
        side_effect=AssertionError("offline decision attempted a network refresh"),
    ) as refresh:
        loaded = features._get_price_for_decision("510300", "2026-07-13")
    assert loaded is not None and len(loaded) == 30
    refresh.assert_not_called()


if __name__ == "__main__":
    test_gate_actions()
    test_news_and_entry_risk_can_veto()
    test_estimator_uses_only_strictly_prior_rows()
    test_offline_price_read_never_refreshes_network()
    print("PROFITABILITY EVIDENCE OK")
