"""Backtest the daily strategy using crawled historical news signals.

This is intentionally simple and transparent:
  - For each trade date, build a strict news signal from SQLite using only
    articles before 09:30.
  - Save it through ``theme_signal.save_theme_signal`` so ``strategy.py`` uses
    the same path as live daily prediction.
  - Run the current short-race strategy.
  - Settle P&L using the platform's own convention: buy at previous trading
    day's close, sell at that day's close (see settlement_prices.py) —
    matches investment-daily-submit.html's stated settlement formula.
"""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pandas as pd

# Historical backtests must not fetch live quotes.  The live daily job keeps its
# own environment and is not affected by this script.
os.environ["ETF_AGENT_STRICT_DATA"] = "1"
os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"

import theme_signal
import llm_client
import llm_decider
from econ_calendar import load_econ_payload
from historical_news_builder import build_historical_signal
from news_signal import summarize_for_llm
from settlement_prices import get_close_to_close
from strategy import (
    OFFENSIVE_POOL,
    OFFENSIVE_ON_THRESHOLD,
    TRADING_POOL,
    _calc_short_race_features,
    _get_price_for_decision,
    market_avg_score,
    run_decision,
    reset_rotation_tracker,
)
from theme_signal import save_theme_signal


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
REPORT_DIR = DATA_DIR / "news_backtest"
BACKTEST_SIGNAL_DIR = REPORT_DIR / "signals"
BACKTEST_AUTO_SIGNAL_PATH = REPORT_DIR / "auto_theme_signal.json"
INITIAL_CAPITAL = 500000.0

# A 组只用东方财富抓取的两个渠道（cbkjj 静态分页 + np350 资讯流 API）。
# B 组加新闻联播；C 组再加经济日历。
SOURCE_PRESETS: dict[str, set[str]] = {
    "eastmoney": {"东方财富-板块聚焦", "东方财富-财经资讯流", "东方财富-财经视点"},
    "cctv": {"cctv_xwlb"},
    "economic": {"baidu_economic"},
}


@contextmanager
def _isolated_signal_dir():
    """Redirect ``theme_signal`` to backtest-only paths so live files stay clean."""
    BACKTEST_SIGNAL_DIR.mkdir(parents=True, exist_ok=True)
    orig_signal_dir = theme_signal.SIGNAL_DIR
    orig_archive_dir = theme_signal.ARCHIVE_DIR
    orig_auto_path = theme_signal.AUTO_SIGNAL_PATH
    theme_signal.SIGNAL_DIR = BACKTEST_SIGNAL_DIR
    theme_signal.ARCHIVE_DIR = BACKTEST_SIGNAL_DIR / "archive"
    theme_signal.AUTO_SIGNAL_PATH = BACKTEST_AUTO_SIGNAL_PATH
    try:
        yield
    finally:
        theme_signal.SIGNAL_DIR = orig_signal_dir
        theme_signal.ARCHIVE_DIR = orig_archive_dir
        theme_signal.AUTO_SIGNAL_PATH = orig_auto_path


def _load_ref_dates(start: str, end: str) -> list[str]:
    path = DATA_DIR / "510300.csv"
    df = pd.read_csv(path).rename(columns={"日期": "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    s = pd.to_datetime(start)
    e = pd.to_datetime(end)
    dates = df[(df["date"] >= s) & (df["date"] <= e)]["date"].dropna()
    return [d.strftime("%Y-%m-%d") for d in dates]


def _bar(code: str, trade_date: str) -> tuple[float, float] | None:
    """返回 (前一交易日收盘价, 当日收盘价)——平台结算口径，见 settlement_prices.py。"""
    return get_close_to_close(code, trade_date, data_dir=DATA_DIR)


def _competition_like_positions(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in result.get("summary", {}).get("held_stocks", []):
        amount = float(item.get("amount") or 0)
        price = float(item.get("latest_price") or 0)
        if amount <= 0 or price <= 0:
            continue
        volume = int(amount // price // 100 * 100)
        if volume <= 0:
            continue
        out.append({
            "symbol": str(item["code"]).zfill(6),
            "symbol_name": item["name"],
            "volume": volume,
            "amount": amount,
            "score": item.get("score"),
        })
    return out


def _build_llm_for_backtest(
    trade_date: str,
    capital: float,
    signal: dict[str, Any],
    *,
    cache_only: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """回测专用：构造 LLM 输入，调 llm_decider；返回 (decision, payload-or-debug)。

    回测时 econ_payload 优先走自身 JSON 缓存 + SQLite，不允许调实时 akshare
    （历史回测要避免对未来数据"作弊"，akshare 接口虽然查历史日期一般安全，
    但保险起见走本地缓存）。
    """
    econ_payload = load_econ_payload(trade_date, allow_live=False)

    if not llm_client.is_available() and not cache_only:
        return None, {"econ_payload": econ_payload, "reason": "no_api_key"}
    if cache_only and not llm_client.is_available():
        # 允许只用缓存：cache_only=True 时即使没 key 也可继续，依赖磁盘命中
        pass

    pool = [dict(item) for item in TRADING_POOL]
    avg_score = market_avg_score(trade_date)
    if avg_score is not None and avg_score >= OFFENSIVE_ON_THRESHOLD:
        pool.extend([dict(item) for item in OFFENSIVE_POOL])

    pool_features: dict[str, dict[str, Any]] = {}
    for item in pool:
        code = str(item["code"]).zfill(6)
        df = _get_price_for_decision(code, trade_date)
        feats = _calc_short_race_features(df)
        if feats:
            pool_features[code] = feats

    summary = summarize_for_llm(signal)
    try:
        payload = llm_decider.decide(
            date_str=trade_date,
            capital=capital,
            pool=pool,
            pool_features=pool_features,
            econ_payload=econ_payload,
            news_signal=signal,
            news_summary=summary,
            use_cache=True,
            cache_only=cache_only,
            save_debug=False,
        )
    except Exception as exc:
        print(f"  [LLM] {trade_date} decide failed: {exc}")
        return None, {"econ_payload": econ_payload, "error": str(exc)}
    if payload is None:
        return None, {"econ_payload": econ_payload, "reason": "decider_none"}
    return payload.get("decision"), {
        "econ_payload": econ_payload,
        "cache_hit": payload.get("cache_hit"),
        "model": payload.get("model"),
        "usage": payload.get("usage"),
        "prompt_hash": payload.get("prompt_hash"),
    }


def simulate_day(
    trade_date: str,
    capital: float,
    *,
    cutoff: str,
    lookback_hours: int,
    channels: set[str] | None,
    signal_out_dir: Path,
    tag: str,
    use_llm: bool = False,
    llm_cache_only: bool = False,
) -> dict[str, Any]:
    signal = build_historical_signal(
        trade_date,
        cutoff_time=cutoff,
        lookback_hours=lookback_hours,
        channels=channels,
        save=True,
        out_dir=signal_out_dir,
        tag=tag,
    )
    save_theme_signal(signal, trade_date)

    llm_decision = None
    llm_debug: dict[str, Any] = {}
    econ_payload = None
    if use_llm:
        llm_decision, llm_debug = _build_llm_for_backtest(
            trade_date, capital, signal, cache_only=llm_cache_only
        )
        econ_payload = llm_debug.get("econ_payload")
    else:
        econ_payload = load_econ_payload(trade_date, allow_live=False)

    result = run_decision(
        trade_date,
        capital,
        llm_decision=llm_decision,
        econ_payload=econ_payload,
    )
    positions = _competition_like_positions(result)

    pnl = 0.0
    settled = []
    for pos in positions:
        bar = _bar(pos["symbol"], trade_date)
        if not bar:
            continue
        prev_close, close_price = bar
        day_pnl = (close_price - prev_close) * int(pos["volume"])
        pnl += day_pnl
        settled.append({
            **pos,
            "prev_close": round(prev_close, 4),
            "close": round(close_price, 4),
            "return_pct": round((close_price / prev_close - 1) * 100, 3) if prev_close else 0.0,
            "pnl": round(float(day_pnl), 2),
        })

    return {
        "date": trade_date,
        "capital_before": round(capital, 2),
        "daily_pnl": round(float(pnl), 2),
        "capital_after": round(capital + pnl, 2),
        "news": {
            "article_count": signal["article_count"],
            "accepted_count": signal["accepted_count"],
            "confidence": signal["confidence"],
            "theme_scores": signal["theme_scores"],
        },
        "econ": {
            "event_count": (econ_payload or {}).get("event_count", 0),
            "high_impact_count": (econ_payload or {}).get("high_impact_count", 0),
            "source": (econ_payload or {}).get("source"),
        },
        "llm": {
            "used": bool(llm_decision),
            "cache_hit": llm_debug.get("cache_hit"),
            "regime": (llm_decision or {}).get("regime"),
            "cash_decision": (llm_decision or {}).get("cash_decision"),
            "position_ratio_hint": (llm_decision or {}).get("position_ratio_hint"),
            "summary_zh": (llm_decision or {}).get("summary_zh"),
            "per_etf_view": (llm_decision or {}).get("per_etf_view", []) if llm_decision else [],
            "hard_rules_applied": (result.get("llm_trace") or {}).get("hard_rules_applied", []),
        },
        "positions": settled,
        "summary": result.get("summary", {}),
    }


def _resolve_channels(sources: list[str]) -> set[str] | None:
    """Map CLI source names to the SQLite ``channel`` strings actually stored."""
    if not sources or "all" in sources:
        return None
    out: set[str] = set()
    for s in sources:
        s_norm = s.strip().lower()
        if s_norm not in SOURCE_PRESETS:
            raise SystemExit(f"unknown source: {s}; choose from {list(SOURCE_PRESETS)} or 'all'")
        out.update(SOURCE_PRESETS[s_norm])
    return out


def run_backtest(
    start: str,
    end: str,
    *,
    cutoff: str,
    lookback_hours: int,
    sources: list[str],
    tag: str,
    use_llm: bool = False,
    llm_cache_only: bool = False,
) -> dict[str, Any]:
    capital = INITIAL_CAPITAL
    rows = []
    channels = _resolve_channels(sources)
    signal_out_dir = REPORT_DIR / "historical_news_signal" / tag
    print(
        f"== run_backtest tag={tag} sources={sources} channels={'ALL' if not channels else sorted(channels)} "
        f"cutoff={cutoff} lookback_hours={lookback_hours} use_llm={use_llm} cache_only={llm_cache_only} ==",
        flush=True,
    )
    reset_rotation_tracker()
    with _isolated_signal_dir():
        for trade_date in _load_ref_dates(start, end):
            row = simulate_day(
                trade_date,
                capital,
                cutoff=cutoff,
                lookback_hours=lookback_hours,
                channels=channels,
                signal_out_dir=signal_out_dir,
                tag=tag,
                use_llm=use_llm,
                llm_cache_only=llm_cache_only,
            )
            capital = float(row["capital_after"])
            rows.append(row)
            picks = " / ".join(f"{p['symbol']}({p['return_pct']:+.2f}%)" for p in row["positions"])
            llm_tag = ""
            if use_llm and row["llm"]["used"]:
                cache_tag = "C" if row["llm"]["cache_hit"] else "N"
                llm_tag = f" llm({row['llm']['regime']}/{row['llm']['cash_decision']}/{cache_tag})"
            print(
                f"[{tag}] {trade_date} pnl={row['daily_pnl']:+,.0f} capital={capital:,.0f} "
                f"news={row['news']['accepted_count']}/{row['news']['article_count']} "
                f"econ={row['econ']['event_count']}/{row['econ']['high_impact_count']}{llm_tag} :: {picks}",
                flush=True,
            )

    pnls = [float(r["daily_pnl"]) for r in rows]
    win_days = sum(1 for v in pnls if v > 0)
    loss_days = sum(1 for v in pnls if v < 0)
    flat_days = sum(1 for v in pnls if v == 0)
    llm_used_days = sum(1 for r in rows if r["llm"]["used"])
    llm_cache_hits = sum(1 for r in rows if r["llm"]["cache_hit"])
    llm_stay_cash_days = sum(1 for r in rows if r["llm"].get("cash_decision") == "stay_cash")
    econ_high_days = sum(1 for r in rows if r["econ"]["high_impact_count"] > 0)
    return {
        "tag": tag,
        "sources": sources,
        "channels": "ALL" if not channels else sorted(channels),
        "start": start,
        "end": end,
        "cutoff": cutoff,
        "lookback_hours": lookback_hours,
        "use_llm": use_llm,
        "llm_stats": {
            "llm_used_days": llm_used_days,
            "llm_cache_hits": llm_cache_hits,
            "llm_stay_cash_days": llm_stay_cash_days,
            "econ_high_impact_days": econ_high_days,
            "llm_token_total": llm_client.stats().get("tokens_used", 0),
            "llm_calls": llm_client.stats().get("calls", 0),
        },
        "initial_capital": INITIAL_CAPITAL,
        "final_capital": round(capital, 2),
        "total_pnl": round(capital - INITIAL_CAPITAL, 2),
        "total_return_pct": round((capital / INITIAL_CAPITAL - 1) * 100, 3),
        "days": len(rows),
        "win_days": win_days,
        "loss_days": loss_days,
        "flat_days": flat_days,
        "win_rate_pct": round(100.0 * win_days / max(1, len(rows)), 2),
        "rows": rows,
    }


def _compare_reports(reports: list[dict[str, Any]]) -> str:
    """Render a small Markdown table comparing two or more reports side-by-side."""
    header = (
        "| tag | total_return | days | win | loss | flat | win_rate | "
        "llm_used | cache_hits | stay_cash | econ_high |"
    )
    sep = "|-----|--------------|------|-----|------|------|----------|----------|-----------|-----------|-----------|"
    lines = [header, sep]
    for r in reports:
        s = r.get("llm_stats") or {}
        lines.append(
            "| {tag} | {tr:+.3f}% | {d} | {w} | {l} | {f} | {wr}% | "
            "{lu} | {ch} | {sc} | {eh} |".format(
                tag=r["tag"],
                tr=r["total_return_pct"],
                d=r["days"], w=r["win_days"], l=r["loss_days"], f=r["flat_days"],
                wr=r["win_rate_pct"],
                lu=s.get("llm_used_days", 0),
                ch=s.get("llm_cache_hits", 0),
                sc=s.get("llm_stay_cash_days", 0),
                eh=s.get("econ_high_impact_days", 0),
            )
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest strategy with crawled historical news.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cutoff", default="09:30")
    parser.add_argument("--lookback-hours", type=int, default=60)
    parser.add_argument(
        "--sources",
        default="all",
        help=(
            "Comma-separated source presets: "
            "eastmoney / cctv / economic / all. Default: all."
        ),
    )
    parser.add_argument("--tag", default="", help="Used to name output report.")
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Run LLM decision fuser (v6) on every backtest day.",
    )
    parser.add_argument(
        "--llm-cache-only",
        action="store_true",
        help="Only use the on-disk LLM cache; never call the live API.",
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run BOTH rule-only and LLM modes, then emit a side-by-side report.",
    )
    args = parser.parse_args()

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    base_tag = args.tag or "+".join(sources)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    if args.compare:
        rule_tag = f"{base_tag}_rule"
        llm_tag = f"{base_tag}_llm"
        rule_report = run_backtest(
            args.start, args.end,
            cutoff=args.cutoff, lookback_hours=args.lookback_hours,
            sources=sources, tag=rule_tag, use_llm=False, llm_cache_only=False,
        )
        llm_report = run_backtest(
            args.start, args.end,
            cutoff=args.cutoff, lookback_hours=args.lookback_hours,
            sources=sources, tag=llm_tag,
            use_llm=True, llm_cache_only=args.llm_cache_only,
        )
        for r in (rule_report, llm_report):
            (REPORT_DIR / f"news_backtest_{args.start}_{args.end}_{r['tag']}.json").write_text(
                json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        cmp_path = REPORT_DIR / f"compare_{args.start}_{args.end}_{base_tag}.md"
        cmp_text = (
            f"# Compare report ({args.start} → {args.end}, sources={sources})\n\n"
            + _compare_reports([rule_report, llm_report])
        )
        cmp_path.write_text(cmp_text, encoding="utf-8")
        print("\nCompare report:", cmp_path)
        print(cmp_text)
        return 0

    report = run_backtest(
        args.start,
        args.end,
        cutoff=args.cutoff,
        lookback_hours=args.lookback_hours,
        sources=sources,
        tag=base_tag,
        use_llm=args.use_llm,
        llm_cache_only=args.llm_cache_only,
    )
    out = REPORT_DIR / f"news_backtest_{args.start}_{args.end}_{base_tag}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nReport:", out)
    print("Total return:", f"{report['total_return_pct']:+.3f}%")
    print(f"Win days: {report['win_days']}/{report['days']} ({report['win_rate_pct']}%)")
    if args.use_llm:
        s = report.get("llm_stats", {})
        print(
            f"LLM: used={s.get('llm_used_days')} cache_hits={s.get('llm_cache_hits')} "
            f"stay_cash={s.get('llm_stay_cash_days')} econ_high={s.get('econ_high_impact_days')} "
            f"tokens={s.get('llm_token_total')} calls={s.get('llm_calls')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
