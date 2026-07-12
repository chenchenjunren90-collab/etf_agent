"""Ensure strategy holdings and competition submit JSON cannot diverge."""

from __future__ import annotations

from daily_job import to_competition_output, validate_execution_consistency
from position import allocate_short_race


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("OK:", message)


def _stock(code: str, price: float) -> dict:
    return {
        "code": code,
        "name": code,
        "score": 60.0,
        "latest_price": price,
    }


def main() -> None:
    dropped = allocate_short_race(
        [_stock("510300", 20.0)],
        total_capital=5000,
        invest_ratio=1.0,
        max_positions=1,
    )
    _assert(not dropped["summary"]["held_stocks"], "sub-lot holding removed before narration")
    _assert(
        dropped["summary"]["execution_dropped"][0]["reason"] == "insufficient_for_one_lot",
        "sub-lot removal is audited",
    )

    valid = allocate_short_race(
        [_stock("510300", 3.0)],
        total_capital=5000,
        invest_ratio=1.0,
        max_positions=1,
    )
    held = valid["summary"]["held_stocks"]
    _assert(held[0]["volume"] == 500, "strategy stores executable lot volume")
    _assert(held[0]["amount"] == 1500.0, "strategy amount matches executable volume")
    submit = to_competition_output(valid)
    validate_execution_consistency(valid, submit)
    _assert(submit[0]["volume"] == held[0]["volume"], "submit matches narrated holding")

    bad_submit = [dict(submit[0], volume=submit[0]["volume"] + 100)]
    try:
        validate_execution_consistency(valid, bad_submit)
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched submit was not rejected")
    print("OK: mismatched submit rejected")
    print("EXECUTION CONSISTENCY OK")


if __name__ == "__main__":
    main()
