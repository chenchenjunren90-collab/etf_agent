"""预热决策大模型磁盘缓存，供 run_news_backtest --use-llm --llm-cache-only 使用。

必须与回测使用相同的「逐日资金曲线」：prompt 中含当日资金，规则版资金序列无法命中大模型版缓存。

推荐用法（需 DEEPSEEK_API_KEY + 联网）::

    py -3 warm_llm_decider_cache.py --start 2026-03-02 --end 2026-04-30

默认按与大模型回测相同的 simulate_day 链路逐日推演资金并写入缓存。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

os.environ.setdefault("ETF_AGENT_STRICT_DATA", "1")
os.environ.setdefault("ETF_AGENT_ALLOW_NETWORK", "1")
os.environ.setdefault("ETF_AGENT_SKIP_NEWS_LLM", "1")

from run_news_backtest import (  # noqa: E402
    INITIAL_CAPITAL,
    REPORT_DIR,
    SOURCE_PRESETS,
    _load_ref_dates,
    _resolve_channels,
    simulate_day,
)
import llm_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Warm LLM decider disk cache for backtest dates.")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--cutoff", default="09:30")
    parser.add_argument("--lookback-hours", type=int, default=60)
    parser.add_argument("--sources", default="all")
    parser.add_argument("--tag", default="warm_cache", help="Signal output subfolder tag.")
    args = parser.parse_args()

    if not llm_client.is_available():
        print("DEEPSEEK_API_KEY 未配置，无法预热缓存。")
        return 1

    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    channels = _resolve_channels(sources)
    dates = _load_ref_dates(args.start, args.end)
    signal_out_dir = REPORT_DIR / "historical_news_signal" / args.tag

    llm_client.reset_stats()
    capital = INITIAL_CAPITAL
    ok = hit = miss = 0
    for trade_date in dates:
        row = simulate_day(
            trade_date,
            capital,
            cutoff=args.cutoff,
            lookback_hours=args.lookback_hours,
            channels=channels,
            signal_out_dir=signal_out_dir,
            tag=args.tag,
            use_llm=True,
            llm_cache_only=False,
        )
        capital = float(row["capital_after"])
        if row["llm"]["cache_hit"]:
            hit += 1
            status = "cache_hit"
        elif row["llm"]["used"]:
            ok += 1
            status = "api_ok"
        else:
            miss += 1
            status = "miss"
        print(
            f"[warm] {trade_date} capital={row['capital_before']:,.0f} "
            f"pnl={row['daily_pnl']:+,.0f} {status}"
        )

    st = llm_client.stats()
    print(
        f"\n完成: {len(dates)} 日 | 新写入 {ok} | 已有缓存 {hit} | 失败 {miss} | "
        f"终资金 {capital:,.0f} | tokens={st['tokens_used']} calls={st['calls']}"
    )
    return 0 if miss == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
