"""ETF 量价特征计算（从 strategy.py 拆出）。

本模块从 strategy.py 忠实拆出，提供：
  - _get_price_for_decision：实盘/回测决策行情获取
  - _load_local_price：单只 ETF CSV 读取
  - _normalize_df：统一列名
  - _calc_short_race_features：短期竞赛全套量价特征
  - _score_to_0_100：分值归一化到 0-100
  - apply_price_confirmation：价格确认过滤
  - _apply_price_confirmation_inline：主题分量价确认（内联版，供 scoring.py 使用）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from indicators import calc_bollinger, calc_macd, calc_momentum, calc_rsi, calc_trend_strength, calc_volume_ratio

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

_SHORT_RACE_PRICE_CONFIRM_CODES = frozenset({
    "510050", "510330", "510300", "510880", "512690", "512010",
})


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名：日期→date, 收盘→close 等。"""
    col_map = {
        "日期": "date", "开盘": "open", "收盘": "close",
        "最高": "high", "最低": "low", "成交量": "volume",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    return df


def _load_local_price(code, date_str=None, limit=120):
    """从本地 CSV 加载 K 线数据。"""
    import glob
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        # 兼容旧命名
        candidates = sorted(glob.glob(str(DATA_DIR / f"*{code}*.csv")))
        if candidates:
            path = Path(candidates[-1])
        else:
            return None
    try:
        df = pd.read_csv(path).rename(columns={
            "日期": "date", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
        })
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
        for col in ["open", "close", "high", "low", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.tail(limit)
    except Exception:
        return None


def _get_price_for_decision(code, date_str=None):
    """实盘/回测决策行情：严格 date < 交易日，不含当日 K 线（避免盘中重跑偷看）。"""
    strict = os.environ.get("ETF_AGENT_STRICT_DATA", "1") == "1"
    if strict or os.environ.get("ETF_AGENT_ALLOW_NETWORK", "0") == "1":
        try:
            from market_data import load_fresh_price

            df = load_fresh_price(code, date_str)
            if df is not None:
                df = _normalize_df(df) if "日期" in df.columns else df
                if date_str:
                    cutoff = pd.to_datetime(date_str, errors="coerce")
                    if pd.notna(cutoff):
                        df = df[df["date"] < cutoff]
                if df is not None and len(df) >= 20:
                    return df.reset_index(drop=True)
        except Exception as e:
            print(f"[FreshData] {code}: {e}")

    df = _load_local_price(code, date_str)
    if df is not None and len(df) >= 20:
        return df.reset_index(drop=True)
    return None


def _score_to_0_100(value, center=0.0, scale=1.0):
    return float(np.clip(50 + (value - center) * scale, 0, 100))


def _calc_short_race_features(df):
    """计算两周比赛更敏感的短周期量价特征。"""
    if df is None or len(df) < 20:
        return None

    close = df["close"].dropna()
    volume = df["volume"].dropna()
    if len(close) < 20 or len(volume) < 20:
        return None

    latest = float(close.iloc[-1])
    ret_1d = float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) >= 2 else 0.0
    ret_3d = float((close.iloc[-1] / close.iloc[-4] - 1) * 100) if len(close) >= 4 else 0.0
    ret_5d = float((close.iloc[-1] / close.iloc[-6] - 1) * 100) if len(close) >= 6 else 0.0
    ret_10d = float((close.iloc[-1] / close.iloc[-11] - 1) * 100) if len(close) >= 11 else 0.0

    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())
    high20 = float(close.tail(20).max())
    low20 = float(close.tail(20).min())

    vol_ratio = calc_volume_ratio(volume, period=20)
    rsi = float(calc_rsi(close).iloc[-1])
    _, _, macd_hist = calc_macd(close)
    bb_pos, bb_width = calc_bollinger(close)

    price_position = (latest - low20) / (high20 - low20 + 1e-10)
    high_break = latest >= high20 * 0.995
    above_ma = latest > ma5 > ma10
    drawdown_5d = float(latest / close.tail(5).max() - 1) * 100

    trend_score = (
        _score_to_0_100(ret_1d, scale=7.0) * 0.15 +
        _score_to_0_100(ret_3d, scale=5.0) * 0.30 +  # 25%→30%
        _score_to_0_100(ret_5d, scale=4.0) * 0.30 +  # 25%→30%
        float(np.clip(vol_ratio * 35, 0, 100)) * 0.10 +  # 15%→10%
        (85.0 if high_break else 55.0) * 0.10 +
        (80.0 if above_ma else 45.0) * 0.05
    )

    risk_penalty = 0.0
    if rsi > 82:
        risk_penalty += min(20.0, (rsi - 82) * 1.2)
    if ret_5d > 12 and bb_pos > 0.9:
        risk_penalty += 12.0
    if bb_width > 0.08:
        risk_penalty += min(15.0, (bb_width - 0.08) * 180)
    if ret_1d < -3 and vol_ratio > 1.5:
        risk_penalty += 10.0

    return {
        "latest_price": latest,
        "ret_1d": round(ret_1d, 2),
        "ret_3d": round(ret_3d, 2),
        "ret_5d": round(ret_5d, 2),
        "ret_10d": round(ret_10d, 2),
        "volume_ratio": round(vol_ratio, 2),
        "rsi": round(rsi, 1),
        "macd_hist": round(macd_hist, 4),
        "bollinger_position": round(bb_pos, 3),
        "bollinger_width": round(bb_width, 4),
        "price_position_20d": round(float(price_position), 3),
        "above_ma": bool(above_ma),
        "high_break": bool(high_break),
        "drawdown_5d": round(drawdown_5d, 2),
        "trend_score": round(float(trend_score), 2),
        "risk_penalty": round(float(risk_penalty), 2),
    }


def _apply_price_confirmation_inline(code, theme_raw, theme_score, features):
    """在新闻主题分之上做温和量价确认：弱盘面时把 theme_score 向中性回收。"""
    if code not in _SHORT_RACE_PRICE_CONFIRM_CODES:
        return float(theme_score)
    tr = float(theme_raw or 0.0)
    if abs(tr) < 0.12:
        return float(theme_score)

    vr = float(features.get("volume_ratio", 1.0) or 1.0)
    ret3 = float(features.get("ret_3d", 0.0) or 0.0)
    aligned = (tr > 0 and ret3 > -0.15) or (tr < 0 and ret3 < 0.15)
    strong_tape = (
        vr >= 1.12
        or bool(features.get("high_break"))
        or (bool(features.get("above_ma")) and ret3 >= -0.05)
    )
    if aligned and strong_tape:
        return float(theme_score)

    fade = 0.62 if abs(tr) >= 0.28 else 0.78
    neutral = 50.0
    ts = float(theme_score)
    adj = neutral + (ts - neutral) * fade
    return float(np.clip(adj, 0, 100))


def apply_price_confirmation(
    ranked: list[dict[str, Any]],
    date_str: str,
) -> list[dict[str, Any]]:
    """对白名单 ETF 做价格确认过滤：当日无价格数据 → 移除。"""
    confirmed = []
    for item in ranked:
        code = item.get("code", "")
        if code in _SHORT_RACE_PRICE_CONFIRM_CODES:
            df = _get_price_for_decision(code, date_str)
            if df is None:
                continue
        confirmed.append(item)
    return confirmed
