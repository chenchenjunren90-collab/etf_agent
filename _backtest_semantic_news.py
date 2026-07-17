"""Point-in-time ablation backtest for the grounded semantic news layer.

The two variants share the current price model, profitability gate, sizing,
risk state and settlement.  Only the news representation differs:

* ``rule_news`` uses the deterministic high-recall keyword/event rules.
* ``semantic_news`` reviews those candidates with the grounded LLM event layer.

Historical articles and price bars are both cut strictly before the decision
timestamp.  The script never persists a live prediction or news signal.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
NEWS_DIR = DATA_DIR / "daily_news_signal"
NEWS_SNAPSHOT_DIR = NEWS_DIR / "snapshots"
CAPITAL = 500_000.0
FRICTION_BPS = 5.0
RETROSPECTIVE_ARTICLES: list[dict[str, Any]] = []

# The bundled desktop Python does not necessarily include python-dotenv.
# Load the existing project environment before importing the LLM modules.
from server_env import load_env_file

load_env_file(BASE_DIR / ".env")

from backtest_provenance import _known_before_cutoff
from daily_job import build_trend_context, to_competition_output, validate_execution_consistency
from decision_integrity import compute_holding_streaks, compute_sole_symbol_streak
from goal_state import summarize_goal_rows
from news_llm_scorer import merge_llm_into_news_signal, score_news_with_llm
from news_signal import build_news_signal
from news_time_split import decision_cutoff, split_articles_by_post_close
from pool import ALL_POOL
from settlement_prices import get_close_to_close
from strategy import reset_rotation_tracker, run_decision


def _trade_dates(start: str, end: str) -> list[str]:
    frame = pd.read_csv(DATA_DIR / "510300.csv")
    date_col = frame.columns[0]
    dates = pd.to_datetime(frame[date_col], errors="coerce").dropna()
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    return [value.strftime("%Y-%m-%d") for value in dates if start_ts <= value <= end_ts]


def _load_point_in_time_articles(trade_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    path = NEWS_DIR / f"{trade_date}.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidates.append(("daily_news_signal", list(payload.get("raw_articles") or []), payload))
        except Exception:
            pass

    full_path = DATA_DIR / "daily_output" / f"{trade_date}_full.json"
    if full_path.exists():
        try:
            full_payload = json.loads(full_path.read_text(encoding="utf-8"))
            full_signal = full_payload.get("news_signal") or {}
            if str(full_payload.get("date") or "")[:10] == trade_date:
                candidates.append(("daily_full_snapshot", list(full_signal.get("raw_articles") or []), full_signal))
        except Exception:
            pass

    snapshot_day = NEWS_SNAPSHOT_DIR / trade_date
    for snapshot_path in sorted(snapshot_day.glob("*.json")) if snapshot_day.exists() else []:
        try:
            document = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot_signal = document.get("signal") or {}
            if str(document.get("trade_date") or "")[:10] != trade_date:
                continue
            candidates.append((
                f"immutable_snapshot:{snapshot_path.name}",
                list(snapshot_signal.get("raw_articles") or []),
                snapshot_signal,
            ))
        except Exception:
            continue

    if not candidates:
        return [], {
            "archive_exists": False,
            "archive_source": "missing",
            "raw_count": 0,
            "accepted_before_cutoff": 0,
            "rejected": {},
        }

    # Later reruns may overwrite daily_news_signal with a smaller feed result.
    # Prefer the richest immutable/full snapshot, while recording every source.
    source, raw, _ = max(candidates, key=lambda item: len(item[1]))
    cutoff = decision_cutoff(trade_date, "09:30")
    retrospective_count = 0
    if RETROSPECTIVE_ARTICLES:
        retrospective_lower = cutoff - timedelta(days=4)
        reconstructed = []
        for article in RETROSPECTIVE_ARTICLES:
            try:
                published = datetime.strptime(
                    str(article.get("published_at") or ""), "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                continue
            if retrospective_lower <= published <= cutoff:
                reconstructed.append(dict(article))
        combined: list[dict[str, Any]] = []
        combined_seen: set[str] = set()
        for article in list(raw) + reconstructed:
            key = str(article.get("url") or "").strip() or (
                f"{article.get('published_at')}|{article.get('source')}|{article.get('title')}"
            )
            if key in combined_seen:
                continue
            combined_seen.add(key)
            combined.append(article)
        raw = combined
        retrospective_count = sum(bool(article.get("retrospective")) for article in raw)
        source = f"{source}+retrospective_search"
    accepted: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    seen: set[str] = set()
    for article in raw:
        key = str(
            article.get("content_sha256")
            or article.get("url")
            or f"{article.get('published_at')}|{article.get('title')}"
        )
        if key in seen:
            rejected["duplicate"] = rejected.get("duplicate", 0) + 1
            continue
        seen.add(key)
        known, reason = _known_before_cutoff(article, cutoff)
        if known:
            accepted.append(dict(article))
        else:
            rejected[reason] = rejected.get(reason, 0) + 1
    return accepted, {
        "archive_exists": True,
        "archive_source": source,
        "archive_candidates": {
            candidate_source: len(candidate_raw)
            for candidate_source, candidate_raw, _ in candidates
        },
        "raw_count": len(raw),
        "retrospective_raw_count": retrospective_count,
        "accepted_before_cutoff": len(accepted),
        "rejected": rejected,
        "decision_cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _process_news(
    articles: list[dict[str, Any]],
    *,
    trade_date: str,
    trend_context: dict[str, Any],
    semantic: bool,
) -> dict[str, Any]:
    signal = build_news_signal(articles, trend_context=trend_context, date=trade_date)
    signal["_original_theme_scores"] = dict(signal.get("theme_scores") or {})
    if not semantic or not signal.get("semantic_candidates"):
        return signal

    pool_codes = [str(item["code"]).zfill(6) for item in ALL_POOL]
    os.environ["TRADE_DATE"] = trade_date
    review = score_news_with_llm(signal["semantic_candidates"], pool_codes)
    if review.get("review_completed"):
        signal = merge_llm_into_news_signal(signal, review)
    else:
        signal["semantic_review_failure"] = dict(review)
    return signal


def _build_news_replay(trade_date: str, *, semantic: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    articles, provenance = _load_point_in_time_articles(trade_date)
    fresh, stale, _ = split_articles_by_post_close(articles, trade_date)
    trend_context = build_trend_context(trade_date)
    fresh_signal = _process_news(
        fresh,
        trade_date=trade_date,
        trend_context=trend_context,
        semantic=semantic,
    )
    # The current trade gate only uses fresh news.  Stale material remains in
    # the audit payload, but skipping its LLM review avoids calls that cannot
    # change the day's allocation when portfolio LLM overrides are disabled.
    stale_signal = _process_news(
        stale,
        trade_date=trade_date,
        trend_context=trend_context,
        semantic=False,
    )
    fresh_articles = list(fresh_signal.get("accepted_articles") or [])
    stale_articles = list(stale_signal.get("accepted_articles") or [])
    semantic_completed = bool(fresh_signal.get("semantic_review_completed"))
    signal = {
        "date": trade_date,
        "source": "point_in_time_semantic_replay" if semantic else "point_in_time_rule_replay",
        "cutoff_time": "09:30",
        "fresh_theme_scores": dict(fresh_signal.get("theme_scores") or {}),
        "stale_theme_scores": dict(stale_signal.get("theme_scores") or {}),
        "theme_scores": dict(fresh_signal.get("theme_scores") or {}),
        "scores": dict(fresh_signal.get("theme_scores") or {}),
        "fresh_accepted_articles": fresh_articles,
        "stale_accepted_articles": stale_articles,
        "accepted_articles": fresh_articles + stale_articles,
        "fresh_accepted_count": len(fresh_articles),
        "stale_accepted_count": len(stale_articles),
        "accepted_count": len(fresh_articles) + len(stale_articles),
        "strong_count": sum(
            1 for item in fresh_articles + stale_articles if item.get("quality") == "strong"
        ),
        "weak_count": sum(
            1 for item in fresh_articles + stale_articles if item.get("quality") != "strong"
        ),
        "confidence": float(fresh_signal.get("confidence") or 0.0),
        "market_sentiment": float(fresh_signal.get("market_sentiment") or 0.0),
        "max_abs_theme": float(fresh_signal.get("max_abs_theme") or 0.0),
        "catalyst_hits": int(fresh_signal.get("catalyst_hits") or 0),
        "semantic_review_completed": semantic_completed,
        "fresh_semantic_review_completed": semantic_completed,
        "semantic_audit": dict(fresh_signal.get("semantic_audit") or {}),
        "fresh_semantic_audit": dict(fresh_signal.get("semantic_audit") or {}),
        "keyword_theme_scores_backup": dict(
            fresh_signal.get("keyword_theme_scores_backup")
            or fresh_signal.get("_original_theme_scores")
            or {}
        ),
        "llm_theme_scores": dict(fresh_signal.get("llm_theme_scores") or {}),
        "auto_news": {
            "enabled": bool(articles),
            "article_count": len(fresh_articles),
            "confidence": float(fresh_signal.get("confidence") or 0.0),
            "market_sentiment": float(fresh_signal.get("market_sentiment") or 0.0),
            "catalyst_hits": int(fresh_signal.get("catalyst_hits") or 0),
            "max_abs_theme": float(fresh_signal.get("max_abs_theme") or 0.0),
        },
    }
    provenance.update({
        "fresh_input_count": len(fresh),
        "stale_input_count": len(stale),
        "fresh_rule_accepted": int(fresh_signal.get("_original_accepted_count", fresh_signal.get("accepted_count", 0)) or 0),
        "fresh_final_accepted": len(fresh_articles),
        "semantic_candidate_count": int(fresh_signal.get("semantic_candidate_count") or 0),
        "semantic_review_completed": semantic_completed,
        "semantic_audit": dict(fresh_signal.get("semantic_audit") or {}),
        "observed_fresh_input_count": sum(
            not bool(article.get("retrospective")) for article in fresh
        ),
        "retrospective_fresh_input_count": sum(
            bool(article.get("retrospective")) for article in fresh
        ),
    })
    observed_fresh = int(provenance["observed_fresh_input_count"])
    retrospective_fresh = int(provenance["retrospective_fresh_input_count"])
    fresh_sources = len({str(article.get("source") or "") for article in fresh})
    if observed_fresh >= 30:
        provenance["coverage_status"] = "complete_observed"
    elif retrospective_fresh >= 8 and fresh_sources >= 3:
        provenance["coverage_status"] = "retrospective_reconstruction"
    elif fresh:
        provenance["coverage_status"] = "sparse"
    else:
        provenance["coverage_status"] = "missing"
    return signal, provenance


def _risk_context(rows: list[dict[str, Any]], trade_date: str) -> dict[str, Any]:
    recent = rows[-5:]
    consecutive_losses = 0
    for row in reversed(recent):
        if float(row["pnl_net"]) < 0:
            consecutive_losses += 1
        else:
            break
    total = sum(float(row["pnl_net"]) for row in recent)
    return {
        "enabled": True,
        "as_of": trade_date,
        "lookback": 5,
        "rows": [
            {"date": row["date"], "pnl": row["pnl_net"], "positions": row["positions"]}
            for row in recent
        ],
        "last_pnl": float(recent[-1]["pnl_net"]) if recent else 0.0,
        "last5_pnl": total,
        "last5_return_pct": total / CAPITAL * 100,
        "consecutive_losses": consecutive_losses,
        "win_rate": (
            sum(float(row["pnl_net"]) > 0 for row in recent) / len(recent)
            if recent else 0.0
        ),
    }


def _integrity_context(rows: list[dict[str, Any]], trade_date: str) -> dict[str, Any]:
    history = [
        {"date": row["date"], "symbols": list(row.get("symbol_list") or [])}
        for row in rows
    ]
    return {
        "price_audit": {
            "decision_date": trade_date,
            "price_stale": False,
            "stale_ratio": 0.0,
            "expected_bar_date": None,
        },
        "price_stale": False,
        "block_llm_rescore": False,
        "recent_submit_history": history[-12:],
        "sole_symbol_streak": compute_sole_symbol_streak(history),
        "holding_streaks": compute_holding_streaks(history),
    }


def _settle(orders: list[dict[str, Any]], trade_date: str) -> tuple[float, float]:
    pnl = 0.0
    used = 0.0
    for order in orders:
        code = str(order.get("symbol") or "").zfill(6)
        volume = int(float(order.get("volume") or 0))
        prices = get_close_to_close(code, trade_date, data_dir=DATA_DIR)
        if volume <= 0:
            continue
        if prices is None:
            raise RuntimeError(f"missing settlement prices: {trade_date} {code}")
        previous_close, close = prices
        pnl += volume * (close - previous_close)
        used += volume * previous_close
    return pnl, used


def _run_variant(
    label: str,
    dates: list[str],
    signals: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    reset_rotation_tracker()
    rows: list[dict[str, Any]] = []
    for index, trade_date in enumerate(dates, 1):
        recent_risk = _risk_context(rows, trade_date)
        integrity = _integrity_context(rows, trade_date)
        block_start = (len(rows) // 10) * 10
        goal_state = summarize_goal_rows(
            trade_date,
            capital=CAPITAL,
            rows=[
                {
                    "date": row["date"],
                    "pnl": row["pnl_net"],
                    "positions": row["positions"],
                }
                for row in rows[block_start:]
            ],
            start_date=rows[block_start]["date"] if block_start < len(rows) else trade_date,
            fixed_window=True,
        )
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            result = run_decision(
                trade_date,
                CAPITAL,
                llm_decision=None,
                recent_risk=recent_risk,
                integrity_ctx=integrity,
                goal_state=goal_state,
                theme_signals_override=signals[trade_date],
            )
        orders = to_competition_output(result)
        validate_execution_consistency(result, orders)
        pnl_gross, used = _settle(orders, trade_date)
        friction = used * FRICTION_BPS / 10_000.0
        pnl_net = pnl_gross - friction
        gate = dict(result.get("profitability_gate") or {})
        rows.append({
            "date": trade_date,
            "pnl_gross": round(pnl_gross, 2),
            "friction": round(friction, 2),
            "pnl_net": round(pnl_net, 2),
            "return_net": pnl_net / CAPITAL,
            "used": round(used, 2),
            "positions": len(orders),
            "symbol_list": [str(item.get("symbol") or "").zfill(6) for item in orders],
            "symbols": ",".join(str(item.get("symbol") or "").zfill(6) for item in orders),
            "mode": (result.get("summary") or {}).get("mode"),
            "market_reason": str(result.get("market_reason") or "")[:240],
            "evidence_version": gate.get("version"),
            "evidence_selected_code": gate.get("selected_code"),
            "cadence": gate.get("cadence"),
        })
        print(
            f"  [{label} {index:02d}/{len(dates)}] {trade_date} "
            f"n={len(orders)} used={used / CAPITAL:5.1%} "
            f"net={pnl_net:+8.2f} {rows[-1]['symbols'] or 'CASH'}",
            flush=True,
        )
    return rows


def _stats(label: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    returns = pd.Series([float(row["return_net"]) for row in rows], dtype=float)
    curve = (1.0 + returns).cumprod()
    drawdown = curve / curve.cummax() - 1.0 if len(curve) else pd.Series(dtype=float)
    trade_rows = [row for row in rows if int(row["positions"]) > 0]
    total_net = sum(float(row["pnl_net"]) for row in rows)
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    return {
        "label": label,
        "days": len(rows),
        "trade_days": len(trade_rows),
        "cash_days": len(rows) - len(trade_rows),
        "winning_trade_days": sum(float(row["pnl_net"]) > 0 for row in trade_rows),
        "losing_trade_days": sum(float(row["pnl_net"]) < 0 for row in trade_rows),
        "trade_win_rate_pct": round(
            sum(float(row["pnl_net"]) > 0 for row in trade_rows) / len(trade_rows) * 100,
            1,
        ) if trade_rows else 0.0,
        "total_pnl_gross": round(sum(float(row["pnl_gross"]) for row in rows), 2),
        "total_friction": round(sum(float(row["friction"]) for row in rows), 2),
        "total_pnl_net": round(total_net, 2),
        "total_return_net_pct": round(total_net / CAPITAL * 100, 3),
        "avg_capital_used_pct": round(
            sum(float(row["used"]) for row in rows) / len(rows) / CAPITAL * 100,
            2,
        ) if rows else 0.0,
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 3) if len(drawdown) else 0.0,
        "sharpe_annualized": round(float(returns.mean()) / std * math.sqrt(252), 2) if std else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest grounded semantic news events")
    parser.add_argument("--start", default="2026-05-25")
    parser.add_argument("--end", default="2026-06-09")
    parser.add_argument(
        "--retrospective-panel",
        help="Optional historical_news_backfill JSON for sensitivity analysis.",
    )
    args = parser.parse_args()

    os.environ["ETF_AGENT_STRICT_DATA"] = "1"
    os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"
    os.environ["ETF_AGENT_STABLE_MODE"] = "1"
    os.environ["ETF_TEN_DAY_GOAL_MODE"] = "monitor"

    if args.retrospective_panel:
        panel_path = Path(args.retrospective_panel)
        panel = json.loads(panel_path.read_text(encoding="utf-8"))
        if panel.get("kind") != "retrospective_search_panel":
            raise SystemExit("invalid retrospective panel kind")
        RETROSPECTIVE_ARTICLES.extend(list(panel.get("articles") or []))
        print(
            f"Retrospective sensitivity panel: {panel_path} "
            f"({len(RETROSPECTIVE_ARTICLES)} articles)"
        )

    dates = _trade_dates(args.start, args.end)
    if not dates:
        raise SystemExit("no settlement dates in requested window")
    print(
        f"Semantic news ablation: {dates[0]} -> {dates[-1]} "
        f"({len(dates)} trading days, friction={FRICTION_BPS:.1f}bps)"
    )

    rule_signals: dict[str, dict[str, Any]] = {}
    semantic_signals: dict[str, dict[str, Any]] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for index, trade_date in enumerate(dates, 1):
        rule_signal, rule_provenance = _build_news_replay(trade_date, semantic=False)
        semantic_signal, semantic_provenance = _build_news_replay(trade_date, semantic=True)
        rule_signals[trade_date] = rule_signal
        semantic_signals[trade_date] = semantic_signal
        provenance[trade_date] = semantic_provenance
        audit = semantic_signal.get("semantic_audit") or {}
        print(
            f"  [news {index:02d}/{len(dates)}] {trade_date} "
            f"raw={semantic_provenance['raw_count']} "
            f"fresh={semantic_provenance['fresh_input_count']} "
            f"rule={semantic_provenance['fresh_rule_accepted']} "
            f"events={audit.get('grounded_event_count', 0)}",
            flush=True,
        )
        # Both variants must use exactly the same historical article set.
        if rule_provenance["accepted_before_cutoff"] != semantic_provenance["accepted_before_cutoff"]:
            raise RuntimeError(f"article provenance mismatch on {trade_date}")

    print("\nRunning allocation variants ...")
    rule_rows = _run_variant("rule", dates, rule_signals)
    semantic_rows = _run_variant("semantic", dates, semantic_signals)
    stats = [_stats("rule_news", rule_rows), _stats("semantic_news", semantic_rows)]

    result = {
        "backtest_kind": (
            "retrospective_news_sensitivity_ablation"
            if args.retrospective_panel else "point_in_time_news_ablation"
        ),
        "strategy_commit": os.popen("git rev-parse --short HEAD").read().strip() or None,
        "start": dates[0],
        "end": dates[-1],
        "trade_days": len(dates),
        "capital": CAPITAL,
        "friction_bps": FRICTION_BPS,
        "portfolio_llm_override": False,
        "notes": [
            "Current strategy and profitability gate are identical across variants.",
            "Semantic LLM is applied only to fresh news because stale news cannot change allocation in this isolated test.",
            (
                "Retrospective search articles are provenance-labelled and used for sensitivity analysis only."
                if args.retrospective_panel
                else "Missing historical news remains missing and causes conservative degradation."
            ),
        ],
        "provenance": provenance,
        "stats": stats,
        "rows": {"rule_news": rule_rows, "semantic_news": semantic_rows},
    }
    complete_days = sum(
        item.get("coverage_status") == "complete_observed" for item in provenance.values()
    )
    reconstructed_days = sum(
        item.get("coverage_status") == "retrospective_reconstruction"
        for item in provenance.values()
    )
    coverage_ratio = complete_days / len(dates)
    usable_ratio = (complete_days + reconstructed_days) / len(dates)
    result["news_coverage"] = {
        "complete_observed_days": complete_days,
        "retrospective_reconstructed_days": reconstructed_days,
        "sparse_days": sum(
            item.get("coverage_status") == "sparse" for item in provenance.values()
        ),
        "missing_days": sum(
            item.get("coverage_status") == "missing" for item in provenance.values()
        ),
        "complete_ratio": round(coverage_ratio, 4),
        "usable_sensitivity_ratio": round(usable_ratio, 4),
        "evidence_status": (
            "credible_window" if coverage_ratio >= 0.90
            else "retrospective_sensitivity_only" if usable_ratio >= 0.90
            else "insufficient_historical_news_coverage"
        ),
    }
    suffix = "retrospective" if args.retrospective_panel else "observed"
    out = DATA_DIR / f"backtest_semantic_news_v8_{suffix}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n=== SUMMARY ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("\n=== NEWS COVERAGE ===")
    print(json.dumps(result["news_coverage"], ensure_ascii=False, indent=2))
    if coverage_ratio < 0.90:
        if usable_ratio >= 0.90:
            print("WARNING: coverage relies on retrospective search; results are sensitivity analysis only.")
        else:
            print("WARNING: historical news coverage is insufficient; returns are exploratory only.")
    print(f"Saved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
