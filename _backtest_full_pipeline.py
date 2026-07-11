"""Full-pipeline backtest: news + LLM + rules + stability + concentration.

Same path as daily_job (minus live price refresh / submit write):
  news_signal → econ → build_llm_decision → run_decision → settle

Uses disk LLM cache when present; only calls DeepSeek on cache miss.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from daily_job import build_llm_decision, to_competition_output
from decision_integrity import compute_holding_streaks, compute_sole_symbol_streak
from econ_calendar import load_econ_payload
from settlement_prices import get_close_to_close
from strategy import reset_rotation_tracker, run_decision
from theme_signal import get_theme_signals, signal_path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
CAPITAL = 500000.0


def _settle(competition_output: list[dict[str, Any]], trade_date: str) -> tuple[float, float]:
    total = 0.0
    used = 0.0
    for item in competition_output:
        code = str(item.get("symbol") or "").zfill(6)
        volume = int(float(item.get("volume") or 0))
        prices = get_close_to_close(code, trade_date, data_dir=DATA_DIR)
        if not code or volume <= 0 or prices is None:
            continue
        prev_close, today_close = prices
        total += volume * (today_close - prev_close)
        used += volume * prev_close
    return float(total), float(used)


def _risk_context(rows: list[dict[str, Any]], as_of: str, lookback: int = 5) -> dict[str, Any]:
    recent = rows[-lookback:]
    consecutive_losses = 0
    for row in reversed(recent):
        if float(row["pnl"]) < 0:
            consecutive_losses += 1
        else:
            break
    total = sum(float(row["pnl"]) for row in recent)
    wins = sum(1 for row in recent if float(row["pnl"]) > 0)
    return {
        "enabled": True,
        "as_of": as_of,
        "lookback": lookback,
        "rows": [
            {"date": row["date"], "pnl": round(float(row["pnl"]), 2), "positions": row["n"]}
            for row in recent
        ],
        "last_pnl": round(float(recent[-1]["pnl"]), 2) if recent else 0.0,
        "last5_pnl": round(total, 2),
        "last5_return_pct": round(total / CAPITAL * 100, 3),
        "consecutive_losses": consecutive_losses,
        "win_rate": round(wins / len(recent), 3) if recent else 0.0,
    }


def _integrity_from_history(rows: list[dict[str, Any]], trade_date: str) -> dict[str, Any]:
    history = []
    for row in rows:
        syms = [s for s in str(row.get("symbols") or "").split(",") if s]
        history.append({"date": row["date"], "symbols": syms})
    streak = compute_sole_symbol_streak(history)
    return {
        "price_audit": {
            "decision_date": trade_date,
            "price_stale": False,
            "stale_ratio": 0.0,
            "expected_bar_date": None,
        },
        "price_stale": False,
        "block_llm_rescore": False,
        "recent_submit_history": history[-6:],
        "sole_symbol_streak": streak,
        "holding_streaks": compute_holding_streaks(history),
    }


def _trade_dates(start: str, end: str) -> list[str]:
    ref = pd.read_csv(DATA_DIR / "510300.csv")
    date_col = ref.columns[0]
    ref[date_col] = pd.to_datetime(ref[date_col], errors="coerce")
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    return [
        d.strftime("%Y-%m-%d")
        for d in ref[date_col].dropna()
        if start_ts <= d <= end_ts
    ]


def _load_news(date_str: str) -> dict[str, Any]:
    path = signal_path(date_str)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # Fallback: theme_signal helper (may return empty structure)
    try:
        return get_theme_signals(date_str) or {}
    except Exception:
        return {"date": date_str, "source": "missing", "theme_scores": {}, "auto_news": {}}


def _load_saved_llm_decision(date_str: str) -> dict[str, Any] | None:
    """Reuse the exact LLM decision from a historical full.json when available.

    Prompt hashes change over time, so disk llm_cache often misses. The
    competition full.json stores the live ``llm_trace`` that was actually used
    that morning — prefer that for a true full-project replay.
    """
    path = DATA_DIR / "daily_output" / f"{date_str}_full.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    trace = data.get("llm_trace")
    if not isinstance(trace, dict):
        trace = (data.get("strategy_result") or {}).get("llm_trace")
    if not isinstance(trace, dict):
        return None
    # Minimal fields strategy.run_decision needs
    if "cash_decision" not in trace and "per_etf_view" not in trace:
        return None
    return trace

def _stats(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    df = pd.DataFrame(rows)
    returns = df["ret"].astype(float)
    total_pnl = float(df["pnl"].sum())
    std = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    curve = (1 + returns).cumprod()
    max_drawdown = float(((curve / curve.cummax()) - 1).min()) if len(curve) else 0.0
    llm_ok = int(df["llm_used"].sum()) if "llm_used" in df.columns else 0
    return {
        "label": label,
        "days": int(len(df)),
        "llm_days": llm_ok,
        "llm_coverage_pct": round(llm_ok / len(df) * 100, 1) if len(df) else 0.0,
        "total_pnl": round(total_pnl, 2),
        "total_ret_pct": round(total_pnl / CAPITAL * 100, 2),
        "win_rate_pct": round(float((returns > 0).mean()) * 100, 1) if len(returns) else 0.0,
        "avg_used_pct": round(float((df["used"] / CAPITAL).mean()) * 100, 1) if len(df) else 0.0,
        "avg_positions": round(float(df["n"].mean()), 2) if len(df) else 0.0,
        "sole_name_days": int((df["n"] == 1).sum()) if len(df) else 0,
        "sharpe_ann": round(float(returns.mean()) / std * math.sqrt(252), 2) if std else 0.0,
        "max_drawdown_pct": round(max_drawdown * 100, 2),
        "last10_pnl": round(float(df.tail(10)["pnl"].sum()), 2) if len(df) else 0.0,
        "last10_ret_pct": round(float(df.tail(10)["pnl"].sum()) / CAPITAL * 100, 2) if len(df) else 0.0,
    }


def run_full_pipeline(
    dates: list[str],
    *,
    cache_only: bool,
) -> list[dict[str, Any]]:
    os.environ["ETF_AGENT_STABLE_MODE"] = "1"
    # Do NOT skip news LLM / decision LLM — full project path.
    os.environ.pop("ETF_AGENT_SKIP_NEWS_LLM", None)

    reset_rotation_tracker()
    rows: list[dict[str, Any]] = []

    for i, trade_date in enumerate(dates, 1):
        news_signal = _load_news(trade_date)
        # Ensure theme file is visible to rank_etfs_short_race via get_theme_signals
        if news_signal and news_signal.get("source") != "missing":
            try:
                from theme_signal import save_theme_signal

                # Avoid rewriting archives every day in BT — write only if missing
                if not signal_path(trade_date).exists():
                    save_theme_signal(news_signal, trade_date)
            except Exception:
                pass

        econ_payload = load_econ_payload(trade_date, allow_live=False, refresh=False)
        recent_risk = _risk_context(rows, trade_date)
        integrity_ctx = _integrity_from_history(rows, trade_date)

        llm_payload = None
        llm_decision = None
        llm_meta = {"used": False, "source": None, "cache_hit": None, "error": None}

        # 1) Prefer historical live LLM decision from full.json (true replay)
        saved = _load_saved_llm_decision(trade_date)
        if saved is not None:
            llm_decision = saved
            llm_meta = {"used": True, "source": "full_json", "cache_hit": True, "error": None}
        else:
            try:
                if cache_only:
                    os.environ["ETF_BT_CACHE_ONLY"] = "1"
                else:
                    os.environ.pop("ETF_BT_CACHE_ONLY", None)

                llm_payload = _build_llm_decision_bt(
                    trade_date, CAPITAL, news_signal, econ_payload, cache_only=cache_only
                )
                if llm_payload:
                    llm_decision = llm_payload.get("decision")
                    llm_meta = {
                        "used": True,
                        "source": "live_or_cache",
                        "cache_hit": llm_payload.get("cache_hit"),
                        "error": None,
                    }
            except Exception as exc:
                llm_meta = {
                    "used": False,
                    "source": None,
                    "cache_hit": None,
                    "error": str(exc)[:120],
                }

        with contextlib.redirect_stdout(io.StringIO()):
            result = run_decision(
                trade_date,
                CAPITAL,
                llm_decision=llm_decision,
                econ_payload=econ_payload,
                recent_risk=recent_risk,
                integrity_ctx=integrity_ctx,
            )
        comp = to_competition_output(result)
        pnl, used = _settle(comp, trade_date)
        conc = result.get("concentration_risk") or {}
        rows.append({
            "date": trade_date,
            "pnl": round(pnl, 2),
            "ret": pnl / CAPITAL,
            "used": used,
            "n": len(comp),
            "symbols": ",".join(item["symbol"] for item in comp),
            "invest_ratio": (result.get("summary") or {}).get("invest_ratio", 0.0),
            "llm_used": bool(llm_meta["used"]),
            "llm_source": llm_meta.get("source"),
            "llm_cache_hit": llm_meta.get("cache_hit"),
            "concentration_applied": bool(conc.get("applied")),
            "mode": (result.get("summary") or {}).get("mode"),
        })
        src = llm_meta.get("source") or "rule"
        err = llm_meta.get("error")
        extra = f" err={err}" if err else ""
        print(
            f"  [{i}/{len(dates)}] {trade_date} "
            f"llm={src} "
            f"n={len(comp)} pnl={pnl:+.0f} {','.join(x['symbol'] for x in comp)}{extra}",
            flush=True,
        )
    return rows


def _build_llm_decision_bt(
    date_str: str,
    capital: float,
    news_signal: dict[str, Any],
    econ_payload: dict[str, Any],
    *,
    cache_only: bool,
) -> dict[str, Any] | None:
    """Same as daily_job.build_llm_decision but can force cache_only."""
    import llm_client
    import llm_decider
    from strategy import (
        OFFENSIVE_ON_THRESHOLD,
        OFFENSIVE_POOL,
        TRADING_POOL,
        _calc_short_race_features,
        _get_price_for_decision,
        market_avg_score,
    )
    from news_signal import summarize_for_llm

    if not llm_client.is_available() and not cache_only:
        return None

    pool = [dict(item) for item in TRADING_POOL]
    avg_score = market_avg_score(date_str)
    if avg_score is not None and avg_score >= OFFENSIVE_ON_THRESHOLD:
        pool.extend([dict(item) for item in OFFENSIVE_POOL])

    pool_features: dict[str, dict[str, Any]] = {}
    for item in pool:
        code = str(item["code"]).zfill(6)
        df = _get_price_for_decision(code, date_str)
        feats = _calc_short_race_features(df)
        if feats:
            pool_features[code] = feats

    news_summary = summarize_for_llm(
        {"accepted_articles": list(news_signal.get("stale_accepted_articles") or [])}
    )
    try:
        payload = llm_decider.decide(
            date_str=date_str,
            capital=capital,
            pool=pool,
            pool_features=pool_features,
            econ_payload=econ_payload,
            news_signal=news_signal,
            news_summary=news_summary,
            use_cache=True,
            cache_only=cache_only,
        )
    except Exception:
        if cache_only:
            return None
        raise
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Full-pipeline ETF backtest (news+LLM+rules)")
    parser.add_argument("--start", default="2026-03-02")
    parser.add_argument("--end", default="2026-07-09")
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="Only use existing LLM cache; never call DeepSeek (cheaper, may miss days).",
    )
    parser.add_argument(
        "--allow-live-llm",
        action="store_true",
        help="On cache miss, call DeepSeek (costs tokens). Default if not --cache-only.",
    )
    args = parser.parse_args()

    os.environ["ETF_AGENT_STRICT_DATA"] = "1"
    os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"  # prices from local CSV only

    # Load .env for DEEPSEEK_API_KEY
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except Exception:
        pass

    cache_only = bool(args.cache_only) or not args.allow_live_llm
    # Default: cache-only first for safety; user can pass --allow-live-llm
    if args.allow_live_llm:
        cache_only = False
    if not args.cache_only and not args.allow_live_llm:
        # Prefer cache-only by default to avoid surprise bill; print hint
        cache_only = True
        print(
            "NOTE: defaulting to --cache-only (use cached LLM decisions). "
            "Pass --allow-live-llm to call DeepSeek on misses.\n"
        )

    dates = _trade_dates(args.start, args.end)
    news_ok = sum(1 for d in dates if signal_path(d).exists())
    print(
        f"Full backtest {args.start} → {args.end} ({len(dates)} days)\n"
        f"  news files: {news_ok}/{len(dates)}\n"
        f"  LLM mode: {'cache_only' if cache_only else 'cache+live'}\n"
    )

    rows = run_full_pipeline(dates, cache_only=cache_only)
    stats = _stats(rows, "full_pipeline_stable_conc")
    print("\n=== SUMMARY ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))

    print("\n=== LAST 15 DAYS ===")
    for row in rows[-15:]:
        if row["llm_used"]:
            llm = row.get("llm_source") or "LLM"
        else:
            llm = "rule"
        flag = " [CONC]" if row.get("concentration_applied") else ""
        print(
            f"{row['date']} {llm:12} n={row['n']} pnl={row['pnl']:+8.1f} "
            f"used={row['used']/CAPITAL*100:4.1f}% {row['symbols']}{flag}"
        )

    out = DATA_DIR / "backtest_full_pipeline.json"
    out.write_text(
        json.dumps(
            {
                "start": args.start,
                "end": args.end,
                "cache_only": cache_only,
                "stats": stats,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nSaved {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
