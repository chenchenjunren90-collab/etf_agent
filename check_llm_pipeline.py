"""大模型链路自检：json 提示、决策缓存覆盖率、可选 API 探活。

用法::

    py -3 check_llm_pipeline.py                    # 离线检查
    py -3 check_llm_pipeline.py --live             # 含一次最小 API 调用
    py -3 check_llm_pipeline.py --backtest-window # 检查回测窗缓存命中率
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("ETF_AGENT_STRICT_DATA", "1")

from llm_client import _ensure_json_hint, is_available, call_json, CACHE_DIR  # noqa: E402
from news_llm_scorer import SYSTEM_PROMPT as NEWS_SYSTEM  # noqa: E402
import llm_decider  # noqa: E402


def _check_json_hints() -> list[str]:
    errors: list[str] = []
    if "json" not in NEWS_SYSTEM.lower():
        errors.append("news_llm_scorer.SYSTEM_PROMPT 缺少 json 字样")
    tpl = llm_decider._load_prompt_template()
    if "json" not in tpl.lower():
        errors.append("decider prompt 模板缺少 json 字样")
    s, p = _ensure_json_hint("纯中文系统说明，不含英文字母", "用户内容")
    if "json" not in (s + p).lower():
        errors.append("llm_client._ensure_json_hint 未生效")
    return errors


def _check_backtest_cache(
    report_path: Path,
    signal_tag: str,
) -> tuple[int, int, list[str]]:
    from run_news_backtest import _build_llm_for_backtest, _load_ref_dates  # noqa: E402

    data = json.loads(report_path.read_text(encoding="utf-8"))
    cap = {r["date"]: float(r["capital_before"]) for r in data.get("rows", [])}
    start, end = data["start"], data["end"]
    sig_dir = Path(__file__).resolve().parent / "data" / "news_backtest" / "historical_news_signal" / signal_tag

    hit = miss = 0
    misses: list[str] = []
    for trade_date in _load_ref_dates(start, end):
        capital = cap.get(trade_date, data.get("initial_capital", 500000))
        hits = sorted(sig_dir.glob(f"{trade_date}_*.json")) or sorted(sig_dir.glob(f"{trade_date}.json"))
        if not hits:
            miss += 1
            misses.append(f"{trade_date}: 无信号文件")
            continue
        signal = json.loads(hits[0].read_text(encoding="utf-8"))
        decision, meta = _build_llm_for_backtest(trade_date, capital, signal, cache_only=True)
        if decision and meta.get("cache_hit"):
            hit += 1
        else:
            miss += 1
            misses.append(f"{trade_date}: 缓存未命中 capital={capital:,.0f}")
    return hit, miss, misses


def _live_ping() -> str | None:
    if not is_available():
        return "DEEPSEEK_API_KEY 未配置，跳过 live 探活"
    try:
        resp = call_json(
            '请返回 json：{"ok": true, "note": "pipeline ping"}',
            schema={"required": ["ok"], "types": {"ok": bool}},
            max_tokens=64,
            date_tag="healthcheck",
            use_cache=True,
            cache_only=False,
            retries=3,
        )
        if resp.get("data", {}).get("ok") is True:
            return None
        return f"live 探活返回异常: {resp.get('data')}"
    except Exception as exc:
        return f"live 探活失败: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF agent LLM pipeline health check")
    parser.add_argument("--live", action="store_true", help="Run one minimal API call")
    parser.add_argument(
        "--backtest-window",
        action="store_true",
        help="Check decider cache coverage for warmed backtest report",
    )
    parser.add_argument(
        "--report",
        default="data/news_backtest/news_backtest_2026-03-02_2026-04-30_llm.json",
    )
    parser.add_argument("--signal-tag", default="llm_backtest")
    args = parser.parse_args()

    print("=== LLM 链路自检 ===\n")
    ok = True

    errs = _check_json_hints()
    if errs:
        ok = False
        print("[FAIL] json 提示词")
        for e in errs:
            print(f"  - {e}")
    else:
        print("[OK] json 提示词（新闻语义分 + 决策模板 + 自动补全）")

    print(f"[{'OK' if is_available() else 'WARN'}] API Key: {'已配置' if is_available() else '未配置（仅 cache-only 可用）'}")
    cache_days = len([d for d in CACHE_DIR.iterdir() if d.is_dir()]) if CACHE_DIR.exists() else 0
    print(f"[INFO] 磁盘缓存目录数: {cache_days}")

    if args.backtest_window:
        report = Path(args.report)
        if report.exists():
            hit, miss, misses = _check_backtest_cache(report, args.signal_tag)
            total = hit + miss
            print(f"[{'OK' if miss == 0 else 'WARN'}] 回测窗决策缓存: {hit}/{total} 命中")
            for m in misses[:10]:
                print(f"  - {m}")
            if miss:
                ok = False
        else:
            print(f"[SKIP] 回测报告不存在: {report}")

    if args.live:
        err = _live_ping()
        if err:
            ok = False
            print(f"[FAIL] {err}")
        else:
            print("[OK] live API 探活")

    print()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
