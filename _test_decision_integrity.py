"""Unit checks for soft near-tie repeat-holding tilt."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from decision_integrity import (
    CLEAR_LEAD_GAP,
    apply_concentration_risk,
    compute_holding_streaks,
    compute_sole_symbol_streak,
)


def _ranked(codes_scores):
    return [{"code": c, "name": c, "score": s} for c, s in codes_scores]


def test_streak_counts():
    hist = [
        {"date": "2026-07-08", "symbols": ["510880"]},
        {"date": "2026-07-09", "symbols": ["510880"]},
    ]
    assert compute_sole_symbol_streak(hist) == {"symbol": "510880", "days": 2}
    assert compute_holding_streaks(hist) == {"510880": 2}


def test_near_tie_tilts_and_may_flip():
    import os

    os.environ["ETF_REPEAT_TILT"] = "1"
    ranked = _ranked([("510880", 55.0), ("512880", 52.5)])  # gap 2.5 < 4
    ctx = {
        "holding_streaks": {"510880": 2},
        "sole_symbol_streak": {"symbol": "510880", "days": 2},
    }
    ranked2, ratio, max_pos, audit = apply_concentration_risk(ranked, 0.55, 1, ctx)
    assert audit["applied"] is True
    assert max_pos == 1
    assert ratio == 0.55  # no size trim
    assert ranked2[0]["code"] == "512880"


def test_default_off_matches_stable_profit_bias():
    import os

    os.environ.pop("ETF_REPEAT_TILT", None)
    ranked = _ranked([("510880", 55.0), ("512880", 52.5)])
    ctx = {
        "holding_streaks": {"510880": 2},
        "sole_symbol_streak": {"symbol": "510880", "days": 2},
    }
    ranked2, ratio, max_pos, audit = apply_concentration_risk(ranked, 0.55, 1, ctx)
    assert audit["mode"] == "disabled"
    assert audit["applied"] is False
    assert ranked2[0]["code"] == "510880"
    assert ranked2[0]["score"] == 55.0


def test_clear_lead_skips_tilt():
    import os

    os.environ["ETF_REPEAT_TILT"] = "1"
    ranked = _ranked([("510880", 60.0), ("512880", 52.0)])  # gap 8 >= 4
    ctx = {
        "holding_streaks": {"510880": 3},
        "sole_symbol_streak": {"symbol": "510880", "days": 3},
    }
    ranked2, ratio, max_pos, audit = apply_concentration_risk(ranked, 0.40, 1, ctx)
    assert audit.get("skipped_clear_lead") is True
    assert audit["applied"] is False
    assert ranked2[0]["code"] == "510880"
    assert ranked2[0]["score"] == 60.0
    assert ratio == 0.40
    assert max_pos == 1
    assert CLEAR_LEAD_GAP == 4.0


def test_no_history():
    ranked = _ranked([("512880", 55.0), ("510880", 52.0)])
    ctx = {"holding_streaks": {}, "sole_symbol_streak": None}
    _, ratio, max_pos, audit = apply_concentration_risk(ranked, 0.55, 1, ctx)
    assert audit["applied"] is False
    assert ratio == 0.55
    assert max_pos == 1


def test_missing_csv_date_is_not_treated_as_holiday():
    import pandas as pd

    import market_data
    from trading_calendar import is_trading_day

    original = market_data._ref_trade_dates
    market_data._ref_trade_dates = lambda: pd.Series(
        pd.to_datetime(["2026-07-08", "2026-07-10"])
    )
    try:
        assert is_trading_day("2026-07-09") is True
    finally:
        market_data._ref_trade_dates = original


def test_fatal_submit_is_excluded_from_holding_history():
    import decision_integrity

    original = decision_integrity.OUTPUT_DIR
    with TemporaryDirectory() as tmp:
        output = Path(tmp)
        decision_integrity.OUTPUT_DIR = output
        try:
            (output / "2026-07-08_submit.json").write_text("[]", encoding="utf-8")
            (output / "2026-07-08_full.json").write_text(
                json.dumps({"mode": "fatal_fallback", "strategy_result": None}),
                encoding="utf-8",
            )
            (output / "2026-07-09_submit.json").write_text(
                json.dumps([{"symbol": "510300", "volume": 100}]),
                encoding="utf-8",
            )
            (output / "2026-07-09_full.json").write_text(
                json.dumps({"mode": "competition", "strategy_result": {"summary": {}}}),
                encoding="utf-8",
            )
            history = decision_integrity.load_recent_submit_history("2026-07-10")
            assert history == [{"date": "2026-07-09", "symbols": ["510300"]}]
        finally:
            decision_integrity.OUTPUT_DIR = original


if __name__ == "__main__":
    test_streak_counts()
    test_default_off_matches_stable_profit_bias()
    test_near_tie_tilts_and_may_flip()
    test_clear_lead_skips_tilt()
    test_no_history()
    test_missing_csv_date_is_not_treated_as_holiday()
    test_fatal_submit_is_excluded_from_holding_history()
    print("OK")
