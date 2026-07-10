"""更新本地 ETF CSV（多源 + 新鲜度校验）。"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))
from market_data import ensure_pool_fresh, latest_completed_trade_date
from pool import ALL_POOL


def update_local_etfs(
    *,
    log_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    # 稳健池 + 进攻池一并刷新，避免宽基强势启用进攻池时用到陈旧 K 线。
    codes = [item["code"] for item in ALL_POOL]
    names = {item["code"]: item["name"] for item in ALL_POOL}
    ok_list, fail_list = ensure_pool_fresh(codes, names, log_fn=log_fn)
    degraded = sum(1 for r in ok_list if r.get("degraded"))
    fresh = sum(1 for r in ok_list if r.get("ok") and not r.get("degraded"))
    return {
        "ok": len(ok_list),
        "fail": len(fail_list),
        "fresh": fresh,
        "degraded": degraded,
        "details": ok_list,
        "fail_details": fail_list,
    }


if __name__ == "__main__":
    print("=" * 50)
    print(f"  更新 {len(ALL_POOL)} 只 ETF（目标已完成交易日 {latest_completed_trade_date()}）")
    print("=" * 50)
    update_local_etfs()
