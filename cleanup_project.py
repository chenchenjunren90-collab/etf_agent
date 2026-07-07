"""清理可再生的临时/实验产物，不影响实盘与正式回测复现。"""
from __future__ import annotations

import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
NB = BASE / "data" / "news_backtest"
HIST = NB / "historical_news_signal"
SIG = NB / "signals"

KEEP_HIST_TAGS = {"llm_backtest"}
KEEP_REPORTS = {
    "backtest_suite_results.json",
    "track_b.json",
    "track_b_2023.json",
    "overlap_plan_comparison.json",
    "news_backtest_2026-03-02_2026-04-30_rule.json",
    "news_backtest_2026-03-02_2026-04-30_llm.json",
}

DELETE_DIRS = [
    NB / "signals_sweep",
    HIST / "warm_llm",
    BASE / "data" / "daily_news_signal" / "archive",
]

DELETE_SCRIPTS: list[str] = []


def main() -> int:
    removed_files = 0
    removed_dirs = 0

    tmp = BASE / "data" / "_0608_extract.txt"
    if tmp.exists():
        tmp.unlink()
        removed_files += 1

    for p in NB.glob("*.log"):
        p.unlink()
        removed_files += 1
    for p in NB.glob("_*.log"):
        p.unlink()
        removed_files += 1

    for p in NB.glob("compare_*.md"):
        p.unlink()
        removed_files += 1
    overlap_md = NB / "overlap_plan_comparison.md"
    if overlap_md.exists():
        overlap_md.unlink()
        removed_files += 1

    for p in NB.glob("news_backtest_*.json"):
        if p.name not in KEEP_REPORTS:
            p.unlink()
            removed_files += 1

    if SIG.exists():
        shutil.rmtree(SIG)
        removed_dirs += 1

    if HIST.exists():
        for d in HIST.iterdir():
            if d.is_dir() and d.name not in KEEP_HIST_TAGS:
                shutil.rmtree(d)
                removed_dirs += 1

    for d in DELETE_DIRS:
        if d.exists():
            shutil.rmtree(d)
            removed_dirs += 1

    for name in DELETE_SCRIPTS:
        p = BASE / name
        if p.exists():
            p.unlink()
            removed_files += 1

    for p in BASE.rglob("__pycache__"):
        if p.is_dir():
            shutil.rmtree(p)
            removed_dirs += 1

    print(f"清理完成: 删除文件约 {removed_files} 个, 目录约 {removed_dirs} 个")
    print(f"保留回测报告: {sorted(KEEP_REPORTS)}")
    print(f"保留历史信号: {sorted(KEEP_HIST_TAGS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
