"""Verify personalization changes holdings by risk/focus."""

from __future__ import annotations

from personalized_advisor import build_personal_advice


def codes(advice):
    return [h["symbol"] for h in advice.get("holdings") or []]


def main():
    base = dict(capital=200000, date_str="2026-07-06", allow_latest_fallback=True)

    a1 = build_personal_advice(**base, risk_preference="conservative", focus="dividend")
    a2 = build_personal_advice(**base, risk_preference="aggressive", focus="growth")
    a3 = build_personal_advice(**base, risk_preference="balanced", focus="sector")
    a4 = build_personal_advice(**base, risk_preference="balanced", focus="auto", prefer_codes=["510880"])
    a5 = build_personal_advice(**base, risk_preference="aggressive", focus="growth", avoid_codes=["512880"])

    print("conservative+dividend:", codes(a1), a1.get("risk_note"))
    print("aggressive+growth:", codes(a2), a2.get("risk_note"))
    print("balanced+sector:", codes(a3))
    print("prefer 510880:", codes(a4))
    print("avoid 512880 growth:", codes(a5))

    assert a1["ok"] and a2["ok"] and a3["ok"]
    # Different styles should not always produce identical baskets
    assert codes(a1) != codes(a2), f"expected different holdings, got {codes(a1)}"
    assert "510880" in codes(a4), "prefer dividend ETF"
    assert "512880" not in codes(a5), "avoid securities ETF"
    # conservative should invest less than aggressive
    inv = lambda a: sum(h.get("approx_amount") or 0 for h in a["holdings"])
    print("invested cons/agg:", round(inv(a1)), round(inv(a2)))
    assert inv(a1) < inv(a2) * 0.85 or inv(a1) + 5000 < inv(a2), (
        f"conservative should use less capital: {inv(a1)} vs {inv(a2)}"
    )
    print("PERSONALIZATION OK")


if __name__ == "__main__":
    main()
