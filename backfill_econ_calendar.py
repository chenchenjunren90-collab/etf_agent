"""回补历史交易日的经济日历缓存（百度接口支持查询过去日期）。

用法:
    py -3 backfill_econ_calendar.py --start 2026-05-01 --end 2026-07-04
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta

from econ_calendar import load_econ_payload, _cache_path


def trade_days(start: str, end: str) -> list[str]:
    d = datetime.strptime(start, "%Y-%m-%d")
    stop = datetime.strptime(end, "%Y-%m-%d")
    out = []
    while d <= stop:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--sleep", type=float, default=1.0)
    args = parser.parse_args()

    days = trade_days(args.start, args.end)
    done = skipped = failed = 0
    for d in days:
        if _cache_path(d).exists():
            skipped += 1
            continue
        try:
            payload = load_econ_payload(d, allow_live=True)
            n = payload.get("event_count", 0)
            src = payload.get("source")
            print(f"{d}: {n} events (source={src})")
            done += 1
        except Exception as exc:
            print(f"{d}: FAILED {exc}")
            failed += 1
        time.sleep(args.sleep)

    print(f"\n回补 {done} 天, 已存在跳过 {skipped} 天, 失败 {failed} 天")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
