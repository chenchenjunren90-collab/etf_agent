"""Quick AkShare connectivity probe."""

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_data import (
    _fetch_akshare,
    _no_proxy_env,
    df_last_date,
    fetch_etf_hist,
    latest_completed_trade_date,
)
from pool import TRADING_POOL

try:
    import akshare as ak
except ImportError as e:
    print("akshare import failed:", e)
    raise SystemExit(1)

print("akshare version:", getattr(ak, "__version__", "unknown"))
print("target trade date:", latest_completed_trade_date())
print("now:", datetime.now())

code = "510300"
print("\n=== raw fund_etf_hist_em 510300 ===")
try:
    with _no_proxy_env():
        raw = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date="20250401",
            end_date="20250709",
            adjust="qfq",
        )
    if raw is None or len(raw) == 0:
        print("empty response")
    else:
        date_col = "日期" if "日期" in raw.columns else "date"
        close_col = "收盘" if "收盘" in raw.columns else "close"
        last = raw.iloc[-1]
        print(f"rows={len(raw)} last_date={last[date_col]} close={last[close_col]}")
except Exception as exc:
    print("EXCEPTION:", type(exc).__name__, exc)
    traceback.print_exc(limit=3)

print("\n=== _fetch_akshare (pool) ===")
start, end = "20250401", "20250709"
for item in TRADING_POOL:
    c = item["code"]
    df = _fetch_akshare(c, start, end)
    if df is None:
        print(c, item["name"], "FAIL")
    else:
        print(c, item["name"], df_last_date(df), round(float(df.iloc[-1]["close"]), 4))

print("\n=== fetch_etf_hist ===")
for item in TRADING_POOL:
    c = item["code"]
    df, src = fetch_etf_hist(c, days=90)
    print(c, src, df_last_date(df) if df is not None else "NONE")
