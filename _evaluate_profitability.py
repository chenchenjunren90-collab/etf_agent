"""Evaluate immutable live outputs separately from current-policy simulations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from settlement_prices import get_close_to_close

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "daily_output"
CAPITAL = 500000.0


def _settle(items: list[dict[str, Any]], trade_date: str) -> tuple[float, float]:
    pnl = 0.0
    used = 0.0
    for item in items:
        code = str(item.get("symbol") or "").zfill(6)
        volume = int(float(item.get("volume") or 0))
        prices = get_close_to_close(code, trade_date, data_dir=DATA_DIR)
        if prices is None or volume <= 0:
            continue
        prev_close, today_close = prices
        pnl += volume * (today_close - prev_close)
        used += volume * prev_close
    return float(pnl), float(used)


def load_live_track_record(
    *,
    start: str | None = None,
    end: str | None = None,
    output_dir: Path = OUTPUT_DIR,
) -> list[dict[str, Any]]:
    """Load only outputs that were actually saved by the daily workflow."""
    rows: list[dict[str, Any]] = []
    start_ts = pd.to_datetime(start, errors="coerce") if start else pd.NaT
    end_ts = pd.to_datetime(end, errors="coerce") if end else pd.NaT
    for path in sorted(output_dir.glob("*_full.json")):
        trade_date = path.name[:10]
        date = pd.to_datetime(trade_date, errors="coerce")
        if pd.isna(date):
            continue
        if pd.notna(start_ts) and date < start_ts:
            continue
        if pd.notna(end_ts) and date > end_ts:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("mode") in {"personal_sandbox", "fatal_fallback"}:
            continue
        items = payload.get("competition_output") or []
        pnl, used = _settle(items, trade_date)
        snapshot = payload.get("decision_snapshot") or {}
        news = payload.get("news_signal") or {}
        accepted = news.get("accepted_articles") or []
        provenance_count = sum(1 for item in accepted if item.get("published_at"))
        rows.append(
            {
                "date": trade_date,
                "pnl": round(pnl, 2),
                "ret": pnl / CAPITAL,
                "used": round(used, 2),
                "symbols": ",".join(
                    str(item.get("symbol") or "").zfill(6) for item in items
                ),
                "positions": len(items),
                "strategy_version": snapshot.get("strategy_version") or "legacy-unversioned",
                "snapshot_sha256": snapshot.get("sha256"),
                "news_accepted": len(accepted),
                "news_with_timestamp": provenance_count,
            }
        )
    return rows


def _benchmark_returns(dates: list[str], code: str = "510300") -> list[float]:
    values: list[float] = []
    for date in dates:
        prices = get_close_to_close(code, date, data_dir=DATA_DIR)
        values.append(prices[1] / prices[0] - 1.0 if prices else 0.0)
    return values


def summarize_rows(
    rows: list[dict[str, Any]],
    *,
    target: float = 0.005,
    cost_bps: float = 5.0,
) -> dict[str, Any]:
    if not rows:
        return {"days": 0, "warning": "no immutable live outputs found"}
    returns = pd.Series([float(row["ret"]) for row in rows], dtype=float)
    used = pd.Series([float(row["used"]) / CAPITAL for row in rows], dtype=float)
    net = returns - used * cost_bps / 10000.0
    benchmark = pd.Series(_benchmark_returns([row["date"] for row in rows]), dtype=float)
    curve = (1.0 + returns).cumprod()
    drawdown = curve / curve.cummax() - 1.0
    rolling10 = [
        float(returns.iloc[i : i + 10].sum())
        for i in range(max(0, len(returns) - 9))
    ]
    nonoverlap10 = [
        float(returns.iloc[i : i + 10].sum())
        for i in range(0, max(0, len(returns) - 9), 10)
    ]
    split = max(1, int(len(rows) * 0.70))
    holdout = returns.iloc[split:]
    timestamps = sum(int(row["news_with_timestamp"]) for row in rows)
    accepted = sum(int(row["news_accepted"]) for row in rows)
    versions = sorted({str(row["strategy_version"]) for row in rows})
    snapshot_coverage = sum(bool(row["snapshot_sha256"]) for row in rows) / len(rows)
    return {
        "kind": (
            "immutable_live_track_record"
            if snapshot_coverage == 1.0
            else "recorded_live_outputs_with_legacy_provenance"
        ),
        "days": len(rows),
        "start": rows[0]["date"],
        "end": rows[-1]["date"],
        "strategy_versions": versions,
        "gross_total_ret_pct": round(float(returns.sum()) * 100, 3),
        "net_total_ret_pct_at_cost_bps": round(float(net.sum()) * 100, 3),
        "cost_bps": cost_bps,
        "benchmark_510300_ret_pct": round(float(benchmark.sum()) * 100, 3),
        "excess_vs_510300_pct": round(float((returns - benchmark).sum()) * 100, 3),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 1),
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 3),
        "target_10d_pct": target * 100,
        "rolling_10d_windows": len(rolling10),
        "rolling_10d_hit_rate_pct": round(
            sum(value >= target for value in rolling10) / len(rolling10) * 100, 1
        ) if rolling10 else 0.0,
        "nonoverlap_10d_windows": len(nonoverlap10),
        "nonoverlap_10d_hit_rate_pct": round(
            sum(value >= target for value in nonoverlap10) / len(nonoverlap10) * 100, 1
        ) if nonoverlap10 else 0.0,
        "holdout_start": rows[split]["date"] if split < len(rows) else None,
        "holdout_ret_pct": round(float(holdout.sum()) * 100, 3) if len(holdout) else 0.0,
        "news_timestamp_coverage_pct": round(timestamps / accepted * 100, 1) if accepted else 0.0,
        "snapshot_coverage_pct": round(snapshot_coverage * 100, 1),
    }


def compare_policy_simulation(
    live_rows: list[dict[str, Any]],
    policy_path: Path,
) -> dict[str, Any]:
    if not policy_path.exists():
        return {"available": False}
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    by_date = {row["date"]: row for row in policy.get("rows") or []}
    comparable = [row for row in live_rows if row["date"] in by_date]
    mismatches = [
        {
            "date": row["date"],
            "live": row["symbols"],
            "simulation": str(by_date[row["date"]].get("symbols") or ""),
        }
        for row in comparable
        if row["symbols"] != str(by_date[row["date"]].get("symbols") or "")
    ]
    return {
        "available": True,
        "comparable_days": len(comparable),
        "symbol_mismatch_days": len(mismatches),
        "symbol_mismatch_rate_pct": round(
            len(mismatches) / len(comparable) * 100, 1
        ) if comparable else 0.0,
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate immutable ETF live outputs")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--target", type=float, default=0.005)
    parser.add_argument("--cost-bps", type=float, default=5.0)
    parser.add_argument(
        "--policy-backtest",
        default=str(DATA_DIR / "backtest_full_pipeline.json"),
    )
    parser.add_argument(
        "--output",
        default=str(DATA_DIR / "profitability_evaluation.json"),
    )
    args = parser.parse_args()

    rows = load_live_track_record(start=args.start, end=args.end)
    report = {
        "live": summarize_rows(rows, target=args.target, cost_bps=args.cost_bps),
        "policy_comparison": compare_policy_simulation(rows, Path(args.policy_backtest)),
        "rows": rows,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
    print(f"Saved {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
