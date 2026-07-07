"""三轨回测汇总：甲轨 K 线、乙轨 CSDN 语料、丙轨东财全链路。"""
from __future__ import annotations

import json
import os
from pathlib import Path

os.environ["ETF_AGENT_STRICT_DATA"] = "1"
os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
OUT = DATA / "news_backtest" / "backtest_suite_results.json"


def track_b_csdn():
    from csdn_backtest import _load_csdn_cache, run_backtest

    cache = _load_csdn_cache()
    if not cache:
        return {"error": "无CSDN缓存"}

    candidates = [
        {"name": "当前采用", "news": 0.35, "trend": 0.30, "hist": 0.20, "risk": 0.15},
        {"name": "新闻30", "news": 0.30, "trend": 0.30, "hist": 0.25, "risk": 0.15},
        {"name": "新闻40", "news": 0.40, "trend": 0.25, "hist": 0.20, "risk": 0.15},
        {"name": "新闻45", "news": 0.45, "trend": 0.25, "hist": 0.20, "risk": 0.10},
    ]
    rows = []
    for c in candidates:
        w = {"news": c["news"], "trend": c["trend"], "hist": c["hist"], "risk": c["risk"]}
        train = run_backtest("2020-01-02", "2021-12-31", cache, w, c["name"] + "_train")
        val = run_backtest("2022-01-04", "2023-12-29", cache, w, c["name"] + "_val")
        rows.append({
            "name": c["name"],
            "weights": w,
            "train": _strip(train),
            "validate": _strip(val),
        })
        print(f"[B] {c['name']} 训练{train['total_return_pct']:+.2f}% 验证{val['total_return_pct']:+.2f}% Sharpe={val['sharpe']:.2f}")

    from scoring import SCORE_GATE
    gates = []
    w0 = {"news": 0.35, "trend": 0.30, "hist": 0.20, "risk": 0.15}
    for gate in [45, 48, 50, 52, 55]:
        import scoring as sc
        old = sc.SCORE_GATE
        sc.SCORE_GATE = float(gate)
        r = run_backtest("2022-01-04", "2023-12-29", cache, w0, f"gate_{gate}")
        sc.SCORE_GATE = old
        gates.append({"gate": gate, **_strip(r)})
        print(f"[B] 闸门{gate} 验证收益{r['total_return_pct']:+.2f}% 胜率{r['win_rate_pct']:.1f}%")
    return {"weight_compare": rows, "score_gate_val": gates, "current_gate": SCORE_GATE}


def track_c_akshare():
    from run_news_backtest import run_backtest

    rule = run_backtest(
        "2026-03-02", "2026-04-30",
        cutoff="09:30", lookback_hours=60,
        sources=["all"], tag="rule", use_llm=False,
    )
    llm = run_backtest(
        "2026-03-02", "2026-04-30",
        cutoff="09:30", lookback_hours=60,
        sources=["all"], tag="llm",
        use_llm=True, llm_cache_only=True,
    )
    return {
        "rule_only": _strip(rule),
        "llm": _strip(llm),
        "llm_stats": llm.get("llm_stats"),
    }


def _strip(r: dict) -> dict:
    return {k: v for k, v in r.items() if k != "rows"}


def main():
    print("=" * 60)
    print("三轨回测汇总")
    print("=" * 60)
    report = {}
    print("\n>>> 乙轨: CSDN 2020-2023")
    report["track_b"] = track_b_csdn()
    print("\n>>> 丙轨: 东财全链路 2026-03~04")
    report["track_c"] = track_c_akshare()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已保存: {OUT}")


if __name__ == "__main__":
    main()
