"""Unit checks for concentration risk (profit-oriented, not ban-same-ETF)."""

from __future__ import annotations

from decision_integrity import (
    SOLE_STREAK_FORCE_2_DAYS,
    apply_concentration_risk,
    compute_sole_symbol_streak,
)


def _ranked(codes_scores):
    return [{"code": c, "name": c, "score": s} for c, s in codes_scores]


def test_streak_counts_sole_days():
    hist = [
        {"date": "2026-07-08", "symbols": ["510880"]},
        {"date": "2026-07-09", "symbols": ["510880"]},
    ]
    assert compute_sole_symbol_streak(hist) == {"symbol": "510880", "days": 2}


def test_day2_forces_two_names_keeps_top():
    ranked = _ranked([("510880", 55.0), ("512880", 52.0), ("510300", 48.0)])
    ctx = {
        "sole_symbol_streak": {"symbol": "510880", "days": 2},
        "price_stale": False,
    }
    ratio, max_pos, audit = apply_concentration_risk(ranked, 0.55, 1, ctx)
    assert audit["applied"] is True
    assert max_pos >= 2
    assert ratio <= 0.35
    # Does NOT swap away #1 — diversification keeps top
    assert ranked[0]["code"] == "510880"


def test_different_top_no_force():
    ranked = _ranked([("512880", 55.0), ("510880", 52.0)])
    ctx = {
        "sole_symbol_streak": {"symbol": "510880", "days": 3},
        "price_stale": False,
    }
    ratio, max_pos, audit = apply_concentration_risk(ranked, 0.55, 1, ctx)
    assert audit["applied"] is False
    assert max_pos == 1
    assert ratio == 0.55


def test_day1_no_force():
    ranked = _ranked([("510880", 55.0), ("512880", 52.0)])
    ctx = {
        "sole_symbol_streak": {"symbol": "510880", "days": 1},
        "price_stale": False,
    }
    _, max_pos, audit = apply_concentration_risk(ranked, 0.55, 1, ctx)
    assert audit["applied"] is False
    assert max_pos == 1
    assert SOLE_STREAK_FORCE_2_DAYS == 2


if __name__ == "__main__":
    test_streak_counts_sole_days()
    test_day2_forces_two_names_keeps_top()
    test_different_top_no_force()
    test_day1_no_force()
    print("OK")
