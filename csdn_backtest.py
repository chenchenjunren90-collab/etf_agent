"""CSDN News Sentiment Backtest — 2020-2023 full window.

Strategy: rule-only (no LLM), uses CSDN stock sentiment → ETF aggregation as news_score.
Compares with existing eastmoney pipeline on the same parameter set.

Architecture:
  final_score = news_score×W_NEWS + trend_score×W_TREND + hist_score×W_HIST - risk×W_RISK
"""

from __future__ import annotations

import json, os, sys, time
from pathlib import Path
from typing import Any

os.environ["ETF_AGENT_STRICT_DATA"] = "1"
os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"

import numpy as np
import pandas as pd

from indicators import calc_momentum, calc_rsi, calc_macd, calc_volume_ratio, calc_trend_strength, calc_bollinger
from features import _get_price_for_decision, _calc_short_race_features
from scoring import score_stock, SCORE_GATE
from position import evaluate_market_regime, short_race_max_positions, allocate_short_race
from strategy import TRADING_POOL, OFFENSIVE_POOL, OFFENSIVE_ON_THRESHOLD, reset_rotation_tracker
from sentiment_aggregator import ETF_INDEX_MAP

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INITIAL_CAPITAL = 500000.0
MIN_AMOUNT = 1000

# ═══════════════════════════════════════════════════
# CSDN score cache loading
# ═══════════════════════════════════════════════════

def _load_csdn_cache() -> dict[str, dict[str, float]]:
    """Load pre-computed CSDN daily ETF scores."""
    cache_file = DATA_DIR / "csdn_scores" / "csdn_daily_scores.json"
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    return {}


def _load_etf_components() -> dict[str, dict[str, float]]:
    """Load ETF component weights."""
    comp_file = DATA_DIR / "etf_components.json"
    if comp_file.exists():
        return json.loads(comp_file.read_text(encoding="utf-8"))
    return {}


# ═══════════════════════════════════════════════════
# Simplified news_score: from CSDN cache
# ═══════════════════════════════════════════════════

def csdn_news_score(code: str, date_str: str, csdn_cache: dict) -> float:
    """Get CSDN-based news sentiment score for an ETF on a date."""
    day_scores = csdn_cache.get(date_str, {})
    return float(day_scores.get(code, 50.0))  # default neutral=50


# ═══════════════════════════════════════════════════
# Single-day simulation
# ═══════════════════════════════════════════════════

def rank_with_csdn(pool, date_str, csdn_cache, weights):
    """Rank ETFs using CSDN sentiment + trend + historical.

    weights: {"news": w1, "trend": w2, "hist": w3, "risk": w4}
    """
    ranked = []
    for item in pool:
        code = item["code"]
        name = item["name"]
        df = _get_price_for_decision(code, date_str)
        features = _calc_short_race_features(df)
        if not features:
            continue

        base = score_stock(df) or {}
        base_score = float(base.get("score", 50.0))
        historical_score = base_score * 0.70  # no ML

        news_score_val = csdn_news_score(code, date_str, csdn_cache)

        final_score = (
            news_score_val * weights["news"] +
            features["trend_score"] * weights["trend"] +
            historical_score * weights["hist"] -
            features.get("risk_penalty", 0.0) * weights["risk"]
        )
        final_score = float(np.clip(final_score, 0, 100))

        ranked.append({
            "code": code, "name": name,
            "score": round(final_score, 2),
            "news_score": round(news_score_val, 2),
            "historical_score": round(historical_score, 2),
            **features,
        })

    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked


def simulate_day(trade_date, capital, csdn_cache, weights, econ_payload=None):
    """Simulate one trading day with CSDN sentiment."""
    pool = [dict(item) for item in TRADING_POOL]
    avg_ret = _market_avg_ret(trade_date)
    if avg_ret is not None and avg_ret >= OFFENSIVE_ON_THRESHOLD:
        pool.extend([dict(item) for item in OFFENSIVE_POOL])

    ranked = rank_with_csdn(pool, trade_date, csdn_cache, weights)

    # Market regime
    invest_ratio, _ = evaluate_market_regime(trade_date)

    # No news-based adjust (CSDN pipeline doesn't use eastmoney)
    # Economic calendar caps
    if econ_payload and econ_payload.get("has_high_impact_event"):
        high_count = econ_payload.get("high_impact_count", 1)
        cap = 0.85 if high_count <= 2 else (0.75 if high_count <= 5 else 0.65)
        invest_ratio = min(invest_ratio, cap)

    # Score gate
    top_score = float(ranked[0]["score"]) if ranked else 0.0
    if invest_ratio > 0 and top_score < SCORE_GATE:
        invest_ratio = 0.0

    # Convert rank to theme_signals format for allocate_short_race
    theme_signals = {"auto_news": {"confidence": 0.0, "market_sentiment": 0.0,
                                    "article_count": 0, "catalyst_hits": 0, "max_abs_theme": 0.0}}
    dyn_max = short_race_max_positions(theme_signals)
    result = allocate_short_race(ranked, capital, invest_ratio, max_positions=dyn_max)

    # Update rotation tracker
    from scoring import _update_rotation_tracker
    _update_rotation_tracker([r["code"] for r in ranked[:3]])

    # P&L
    pnl = 0.0
    for item in result.get("summary", {}).get("held_stocks", []):
        bar = _get_bar(item["code"], trade_date)
        if not bar:
            continue
        open_p, close_p = bar
        amount = float(item.get("amount", 0))
        price = float(item.get("latest_price", 0))
        if amount <= 0 or price <= 0:
            continue
        vol = int(amount // price // 100 * 100)
        if vol <= 0:
            continue
        pnl += (close_p - open_p) * vol

    return {
        "date": trade_date, "pnl": round(pnl, 2),
        "capital_after": round(capital + pnl, 2),
        "invest_ratio": invest_ratio, "positions": len(ranked),
    }


def _market_avg_ret(date_str):
    refs = ["510300", "159915", "588000"]
    scores = []
    for code in refs:
        df = _get_price_for_decision(code, date_str)
        features = _calc_short_race_features(df)
        if features:
            scores.append(features["ret_5d"] + features["ret_3d"] * 0.5)
    return float(np.mean(scores)) if scores else None


def _get_bar(code, trade_date):
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path).rename(columns={"日期": "date", "开盘": "open", "收盘": "close"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    row = df[df["date"] == pd.to_datetime(trade_date)]
    if row.empty:
        return None
    return float(row.iloc[0]["open"]), float(row.iloc[0]["close"])


def _load_trading_dates(start, end):
    path = DATA_DIR / "510300.csv"
    df = pd.read_csv(path)
    col = "日期" if "日期" in df.columns else df.columns[0]
    df[col] = pd.to_datetime(df[col], errors="coerce")
    mask = (df[col] >= pd.to_datetime(start)) & (df[col] <= pd.to_datetime(end))
    return [d.strftime("%Y-%m-%d") for d in df.loc[mask, col].dropna()]


# ═══════════════════════════════════════════════════
# Backtest runner
# ═══════════════════════════════════════════════════

def run_backtest(start, end, csdn_cache, weights, tag=""):
    """Run full backtest for a date range with given weights."""
    capital = INITIAL_CAPITAL
    rows = []
    reset_rotation_tracker()
    dates = _load_trading_dates(start, end)

    for trade_date in dates:
        row = simulate_day(trade_date, capital, csdn_cache, weights)
        capital = row["capital_after"]
        rows.append(row)
        if len(rows) % 30 == 0:
            ret = (capital / INITIAL_CAPITAL - 1) * 100
            print(f"[{tag}] {trade_date} | pnl={row['pnl']:+,.0f} | cum_return={ret:+.2f}%", flush=True)

    pnls = [r["pnl"] for r in rows]
    win = sum(1 for v in pnls if v > 0)
    loss = sum(1 for v in pnls if v < 0)
    flat = sum(1 for v in pnls if v == 0)

    cummax = 0.0; cumsum = 0.0; maxdd = 0.0
    for p in pnls:
        cumsum += p
        cummax = max(cummax, cumsum)
        maxdd = max(maxdd, cummax - cumsum)

    daily_rets = [p / INITIAL_CAPITAL for p in pnls]
    sharpe = np.mean(daily_rets) / max(np.std(daily_rets), 1e-10) * np.sqrt(252)

    return {
        "tag": tag, "start": start, "end": end,
        "total_return_pct": round((capital / INITIAL_CAPITAL - 1) * 100, 3),
        "final_capital": round(capital, 2),
        "days": len(rows), "win": win, "loss": loss, "flat": flat,
        "win_rate_pct": round(100 * win / max(1, len(rows)), 2),
        "max_drawdown": round(maxdd, 2), "sharpe": round(sharpe, 3),
        "weights": weights,
        "rows": rows,
    }


# ═══════════════════════════════════════════════════
# Grid search
# ═══════════════════════════════════════════════════

def grid_search(start, end, csdn_cache):
    """Grid search optimal weights."""
    results = []

    # Search space
    w_news_list = [0.30, 0.35, 0.40, 0.45, 0.50]
    w_trend_list = [0.20, 0.25, 0.30]
    w_hist_list = [0.10, 0.15, 0.20]
    w_risk_list = [0.10]

    total_combos = (len(w_news_list) * len(w_trend_list) * len(w_hist_list) * len(w_risk_list))
    print(f"Grid search: {len(w_news_list)}×{len(w_trend_list)}×{len(w_hist_list)} = {total_combos} combos")
    i = 0

    for wn in w_news_list:
        for wt in w_trend_list:
            for wh in w_hist_list:
                for wr in w_risk_list:
                    if abs(wn + wt + wh + wr - 1.0) > 0.01:
                        continue
                    i += 1
                    tag = f"n{int(wn*100)}_t{int(wt*100)}_h{int(wh*100)}"
                    print(f"\n[{i}] {tag}...")

                    w = {"news": wn, "trend": wt, "hist": wh, "risk": wr}
                    r = run_backtest(start, end, csdn_cache, w, tag)
                    print(f"  Return={r['total_return_pct']:+.3f}% Sharpe={r['sharpe']:.3f} Win={r['win_rate_pct']}%")

                    results.append(r)

    # Sort by Sharpe
    results.sort(key=lambda x: x["sharpe"], reverse=True)

    print("\n" + "=" * 60)
    print("TOP RESULTS")
    print("=" * 60)
    print(f"{'Rank':<5} {'Tag':<20} {'Return':>8} {'Sharpe':>8} {'WinRate':>8} {'MaxDD':>8}")
    for rank, r in enumerate(results[:10]):
        w = r["weights"]
        tag = f"n{w['news']:.0%}_t{w['trend']:.0%}_h{w['hist']:.0%}"
        print(f"{rank+1:<5} {tag:<20} {r['total_return_pct']:+7.3f}% {r['sharpe']:+8.3f} "
              f"{r['win_rate_pct']:>7}% {r['max_drawdown']:>8,.0f}")

    return results


# ═══════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("CSDN SENTIMENT BACKTEST — 2020-2023")
    print("=" * 60)

    # 1. Check if CSDN cache exists
    cache_file = DATA_DIR / "csdn_scores" / "csdn_daily_scores.json"
    if not cache_file.exists():
        print("\nBuilding CSDN score cache first...")
        from sentiment_aggregator import build_csdn_score_cache, load_csdn_range, fetch_index_components
        components = fetch_index_components(use_cache=True)
        csdn_df = load_csdn_range(2020, 2023)
        build_csdn_score_cache(csdn_df, components)
        print("Cache built!")

    csdn_cache = _load_csdn_cache()
    print(f"CSDN cache: {len(csdn_cache)} dates loaded")
    if len(csdn_cache) == 0:
        print("ERROR: No CSDN data. Run sentiment_aggregator.py first.")
        return

    # Quick single-weight test
    print("\n--- Quick Test (news=45%, trend=25%, hist=20%, risk=10%) ---")
    w_test = {"news": 0.45, "trend": 0.25, "hist": 0.20, "risk": 0.10}
    r = run_backtest("2020-01-02", "2023-12-29", csdn_cache, w_test, "quick")
    print(f"  2020-2023: Return={r['total_return_pct']:+.3f}%, Sharpe={r['sharpe']:.3f}, "
          f"Win={r['win_rate_pct']}%, {r['days']} days")

    # Grid search on 2020-2021 (train), validate on 2022-2023
    print("\n--- Grid Search (2020-2021 train) ---")
    results = grid_search("2020-01-02", "2021-12-31", csdn_cache)

    # Best params validation
    if results:
        best = results[0]
        print(f"\n--- Best Params Validation (2022-2023) ---")
        val = run_backtest("2022-01-04", "2023-12-29", csdn_cache, best["weights"], "best_validate")
        print(f"  Train Return={best['total_return_pct']:+.3f}% | "
              f"Val Return={val['total_return_pct']:+.3f}% | "
              f"Sharpe={val['sharpe']:.3f}")

        # Save full results
        report = {
            "best_weights": best["weights"],
            "train_result": {k: v for k, v in best.items() if k != "rows"},
            "validate_result": {k: v for k, v in val.items() if k != "rows"},
            "all_results": [{k: v for k, v in r.items() if k != "rows"} for r in results[:20]],
        }
        out = DATA_DIR / "news_backtest" / "csdn_grid_search.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report saved: {out}")


if __name__ == "__main__":
    main()
