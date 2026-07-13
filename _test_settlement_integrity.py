"""Regression checks for calendar-aligned and complete settlement data."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from settlement_prices import get_close_to_close
from stability_risk import build_recent_risk_context


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("OK:", message)


def _write_prices(root: Path, code: str, rows: list[dict]) -> None:
    pd.DataFrame(rows).to_csv(root / f"{code}.csv", index=False)


def main() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        valid_rows = [
            {"date": "2026-07-08", "open": 2.99, "high": 3.02, "low": 2.98, "close": 3.00, "volume": 900},
            {"date": "2026-07-09", "open": 3.00, "high": 3.04, "low": 2.99, "close": 3.01, "volume": 1000},
            {"date": "2026-07-10", "open": 3.01, "high": 3.05, "low": 2.98, "close": 3.04, "volume": 1200},
        ]
        _write_prices(root, "510300", valid_rows)
        prices = get_close_to_close(
            "510300",
            "2026-07-10",
            data_dir=root,
            as_of=datetime(2026, 7, 10, 16, 30),
        )
        _assert(prices == (3.01, 3.04), "calendar-aligned previous close")

        _write_prices(
            root,
            "510500",
            [valid_rows[0], valid_rows[2]],
        )
        _assert(
            get_close_to_close(
                "510500",
                "2026-07-10",
                data_dir=root,
                as_of=datetime(2026, 7, 10, 16, 30),
            )
            is None,
            "missing calendar previous day is rejected",
        )
        _assert(
            get_close_to_close(
                "510300",
                "2026-07-10",
                data_dir=root,
                as_of=datetime(2026, 7, 10, 15, 30),
            )
            is None,
            "same-day bar remains pending before readiness cutoff",
        )

        low_volume_rows = [
            valid_rows[0],
            valid_rows[1],
            {
                "date": "2026-07-10",
                "open": 3.01,
                "high": 3.01,
                "low": 3.01,
                "close": 3.01,
                "volume": 10,
            },
        ]
        _write_prices(root, "510880", low_volume_rows)
        _assert(
            get_close_to_close(
                "510880",
                "2026-07-10",
                data_dir=root,
                as_of=datetime(2026, 7, 10, 16, 30),
            )
            is None,
            "low-volume intraday residue is rejected after cutoff",
        )

        output = root / "outputs"
        output.mkdir()
        (output / "2026-07-08_full.json").write_text(
            json.dumps({"mode": "fatal_fallback", "competition_output": []}),
            encoding="utf-8",
        )
        (output / "2026-07-09_full.json").write_text(
            json.dumps(
                {
                    "mode": "competition",
                    "competition_output": [
                        {"symbol": "510300", "volume": 100, "symbol_name": "沪深300ETF"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        context = build_recent_risk_context(
            "2026-07-10",
            capital=500000,
            output_dir=output,
            data_dir=root,
        )
        _assert([row["date"] for row in context["rows"]] == ["2026-07-09"], "fatal fallback excluded from risk history")

    print("SETTLEMENT INTEGRITY OK")


if __name__ == "__main__":
    main()
