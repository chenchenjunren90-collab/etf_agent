"""Fetch extended K-line data for all ETF pool members from AKShare.

Extends existing CSV files back to 2020-01-01, keeping the 2024+ data intact.
"""
import pandas as pd
import time
import sys
from pathlib import Path

from market_data import _no_proxy_env

DATA_DIR = Path(__file__).resolve().parent / "data"

POOL_CODES = [
    # Main pool
    "510300", "510050", "510500", "510330", "159338",
    "518880", "159985", "510880", "512880", "512010",
    # Offensive pool
    "159915", "588000", "159949",
]

START_DATE = "20200101"  # Back to 2020

def fetch_and_save(code: str):
    """Fetch ETF daily data and merge with existing CSV."""
    path = DATA_DIR / f"{code}.csv"

    with _no_proxy_env():
        import akshare as ak
        # Fetch full history
        df_new = ak.fund_etf_hist_em(
            symbol=code,
            period="daily",
            start_date=START_DATE,
            end_date="20260609",
            adjust="qfq",  # 前复权
        )

    # Normalize columns to match existing CSVs
    col_map = {
        "日期": "日期",
        "开盘": "开盘",
        "收盘": "收盘",
        "最高": "最高",
        "最低": "最低",
        "成交量": "成交量",
        "成交额": "成交额",
        "振幅": "振幅",
        "涨跌幅": "涨跌幅",
        "涨跌额": "涨跌额",
        "换手率": "换手率",
    }
    df_new = df_new.rename(columns=col_map)
    # Keep only columns that exist in both
    keep_cols = ["日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额", "振幅", "涨跌幅", "涨跌额", "换手率"]
    df_new = df_new[[c for c in keep_cols if c in df_new.columns]]

    # Read existing CSV if it exists
    if path.exists():
        df_old = pd.read_csv(path)
        # Ensure same columns
        df_old = df_old[[c for c in keep_cols if c in df_old.columns]]
        # Merge: new data first, then deduplicate by date
        df_merged = pd.concat([df_new, df_old], ignore_index=True)
    else:
        df_merged = df_new

    # Remove duplicates by date
    df_merged = df_merged.drop_duplicates(subset=["日期"], keep="last")
    df_merged = df_merged.sort_values("日期").reset_index(drop=True)

    # Save
    df_merged.to_csv(path, index=False, encoding="utf-8-sig")
    return len(df_merged), df_merged["日期"].iloc[0], df_merged["日期"].iloc[-1]


for i, code in enumerate(POOL_CODES):
    try:
        rows, start, end = fetch_and_save(code)
        print(f"[{i+1}/{len(POOL_CODES)}] {code}: {rows} rows, {start} ~ {end}")
    except Exception as e:
        print(f"[{i+1}/{len(POOL_CODES)}] {code}: FAIL - {e}")
    time.sleep(0.3)  # Be polite to API

print("\nDone. Check data/ directory for updated CSVs.")
