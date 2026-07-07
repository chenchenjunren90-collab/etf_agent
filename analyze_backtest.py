"""回测报告分析器：统计功效 + 仓位分布 + 信号饱和度 + 等权基线对比。

用法:
    py -3 analyze_backtest.py data/news_backtest/news_backtest_2026-03-02_2026-07-03_rule_topk.json
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

from settlement_prices import get_close_to_close

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

BASELINE_POOL = ["510300", "510050", "510500", "510330", "159338", "510880", "512880", "512010"]


def close_to_close_baseline(dates: list[str], codes: list[str]) -> float:
    """等权持有 codes、每天昨收→今收（平台结算口径）的累计收益（%）。"""
    rets_by_date: dict[str, list[float]] = {}
    for code in codes:
        for d in dates:
            prices = get_close_to_close(code, d, data_dir=DATA_DIR)
            if prices is None:
                continue
            prev_close, close = prices
            rets_by_date.setdefault(d, []).append(close / prev_close - 1)
    eq = 1.0
    for d in dates:
        rs = rets_by_date.get(d, [])
        if rs:
            eq *= 1 + sum(rs) / len(rs)
    return (eq - 1) * 100


def main(report_path: str) -> int:
    j = json.loads(Path(report_path).read_text(encoding="utf-8"))
    rows = j["rows"]
    dates = [r["date"] for r in rows]
    n = len(rows)

    rets, ratios = [], []
    sat_days = 0
    for r in rows:
        pnl = float(r["daily_pnl"] or 0)
        cb = float(r["capital_before"])
        rets.append(pnl / cb * 100)
        s = r.get("summary") or {}
        used = float(s.get("capital_used", 0) or 0)
        ratios.append(used / cb if cb else 0.0)
        ts = (r.get("news") or {}).get("theme_scores") or {}
        if sum(1 for v in ts.values() if abs(float(v)) >= 0.8) >= 6:
            sat_days += 1

    mean = sum(rets) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / (n - 1))
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    sharpe = mean / sd * math.sqrt(244) if sd > 0 else 0.0

    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in rets:
        eq *= 1 + x / 100
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak)

    pos = [x for x in rets if x > 0.001]
    neg = [x for x in rets if x < -0.001]

    print(f"报告: {Path(report_path).name}")
    print(f"窗口: {dates[0]} ~ {dates[-1]}  ({n} 交易日)")
    print(f"总收益: {j.get('total_return_pct')}%   胜率: {j.get('win_rate_pct')}%")
    print(f"日均 {mean:+.4f}%  日波动 {sd:.4f}%  t={t:.2f}  年化夏普 {sharpe:.2f}  最大回撤 {mdd*100:.2f}%")
    if pos and neg:
        print(f"平均盈利日 +{sum(pos)/len(pos):.3f}%  平均亏损日 {sum(neg)/len(neg):.3f}%  "
              f"盈亏比 {abs((sum(pos)/len(pos))/(sum(neg)/len(neg))):.2f}")
    print(f"平均资金占用 {sum(ratios)/n:.1%}   空仓日 {sum(1 for x in ratios if x <= 0.001)}")
    print(f"主题分饱和日(>=6只顶格0.8+): {sat_days}/{n}")

    base = close_to_close_baseline(dates, BASELINE_POOL)
    print(f"\n基线-等权池({len(BASELINE_POOL)}只)昨收→今收(平台口径): {base:+.2f}%")
    strat = float(j.get("total_return_pct") or 0)
    print(f"策略 vs 基线: {strat - base:+.2f} 个百分点  {'跑赢' if strat > base else '跑输'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else
                          str(DATA_DIR / "news_backtest" / "news_backtest_2026-03-02_2026-07-03_rule_full.json")))
