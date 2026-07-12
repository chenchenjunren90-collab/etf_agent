"""闭市后自动同步：更新 ETF 行情 CSV，供仪表盘结算当日预测收益。

A 股 15:00 收盘；数据源通常在 16:00 后才有完整日 K。
由 crontab 在交易日 16:15 / 16:45 / 17:15 触发（见 scripts/post_close_sync.sh）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("ETF_AGENT_ALLOW_NETWORK", "1")

from daily_pnl import review_previous_prediction, write_pnl_report
from market_data import csv_last_date, latest_completed_trade_date
from pool import ALL_POOL
from update_local_csv import update_local_etfs

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
LOG_PATH = OUTPUT_DIR / "post_close_sync.log"
STATUS_PATH = OUTPUT_DIR / "post_close_sync_status.json"

MAX_ATTEMPTS = 3
RETRY_SLEEP_SEC = 300


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _verify_pool_csv(target) -> tuple[int, int]:
    ok = fail = 0
    missing: list[str] = []
    for item in ALL_POOL:
        code = str(item["code"]).zfill(6)
        last = csv_last_date(code)
        if last is not None and last >= target:
            ok += 1
        else:
            fail += 1
            missing.append(f"{code}({item['name']})")
    if missing:
        log(f"  行情仍缺 {target}: {', '.join(missing)}")
    return ok, fail


def _write_status(payload: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    target = latest_completed_trade_date()
    log("=" * 50)
    log(f"闭市后行情同步开始，目标交易日 {target}")

    last_ok = last_fail = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        log(f"第 {attempt}/{MAX_ATTEMPTS} 次拉取行情…")
        stats = update_local_etfs(log_fn=log)
        ok_n = int(stats.get("ok", 0))
        fail_n = int(stats.get("fail", 0))
        log(f"  拉取结果: 成功 {ok_n}，失败 {fail_n}")
        csv_ok, csv_fail = _verify_pool_csv(target)
        last_ok, last_fail = csv_ok, csv_fail
        if csv_fail == 0:
            break
        if attempt < MAX_ATTEMPTS:
            log(f"  {RETRY_SLEEP_SEC}s 后重试…")
            time.sleep(RETRY_SLEEP_SEC)

    # review_previous_prediction uses an exclusive cutoff. Post-close must
    # include today's prediction, so pass tomorrow rather than settling yesterday again.
    review_cutoff = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    pnl_report = review_previous_prediction(review_cutoff)
    pnl_path = write_pnl_report(pnl_report)
    if pnl_report:
        if pnl_report.get("pending"):
            log(
                f"当日预测 {pnl_report['prediction_date']} 尚未完整结算: "
                + ", ".join(pnl_report.get("unsettled_symbols") or [])
            )
        else:
            log(
                f"当日预测复盘: {pnl_report['prediction_date']} "
                f"收益 {pnl_report['total_pnl']:+.2f} 元"
            )
    else:
        log("暂无可复盘的上一日预测")

    status = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "target_trade_date": str(target),
        "csv_ok": last_ok,
        "csv_fail": last_fail,
        "csv_complete": last_fail == 0,
        "pnl_report_path": str(pnl_path),
        "previous_pnl": pnl_report,
    }
    _write_status(status)

    settlement_pending = bool(pnl_report and pnl_report.get("pending"))
    if last_fail > 0 or settlement_pending:
        log(f"同步未完全成功：{last_fail}/{last_ok + last_fail} 只 ETF 仍缺 {target} 收盘")
        if settlement_pending:
            log("结算行情仍不完整，等待下一轮闭市同步重试")
        log("=" * 50)
        return 1

    log("闭市后行情同步完成")
    log("=" * 50)
    return 0


if __name__ == "__main__":
    sys.exit(main())
