"""技术指标计算函数集合（RSI / MACD / 动量 / 量比 / 趋势强度 / 布林带）。

本模块从 strategy.py 拆出，不依赖项目内其他模块，
仅使用 pandas / numpy。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Relative Strength Index)"""
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - (100 / (rs + 1))


def calc_macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD → (macd_line, signal_line, histogram)"""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])


def calc_momentum(close: pd.Series, period: int = 5) -> float:
    """动量：N 日收益率 (%)"""
    if len(close) < period:
        return 0.0
    return float((close.iloc[-1] / close.iloc[-period] - 1) * 100)


def calc_volume_ratio(vol: pd.Series, period: int = 20) -> float:
    """量比：当日成交量 / 过去 N 日均值"""
    if len(vol) < 5:
        return 1.0
    avg = vol[:-1].tail(period).mean() if len(vol) > 1 else vol.mean()
    return float(vol.iloc[-1] / (avg + 1e-10))


def calc_trend_strength(close: pd.Series, period: int = 20) -> float:
    """趋势强度：价格在 N 日均线上方的比例 (0~1)"""
    if len(close) < period:
        return 0.0
    ma = close.rolling(period).mean()
    return float((close.tail(period) > ma.tail(period)).sum() / period)


def calc_bollinger(close: pd.Series, period: int = 20, nb_std: float = 2):
    """布林带因子 → (bb_position, bb_width)

    - bb_position: 价格在布林带中的相对位置 (0=下轨, 0.5=中轨, 1=上轨)
    - bb_width: 布林带宽度（波动率代理）
    """
    if len(close) < period:
        return 0.5, 0.0

    ma = close.rolling(period).mean()
    std = close.rolling(period).std()

    upper = ma + nb_std * std
    lower = ma - nb_std * std

    latest = close.iloc[-1]
    latest_upper = float(upper.iloc[-1])
    latest_lower = float(lower.iloc[-1])
    latest_mid = float(ma.iloc[-1])

    width = float(std.iloc[-1] / (latest_mid + 1e-10))

    if latest_upper == latest_lower:
        position = 0.5
    else:
        position = float(np.clip((latest - latest_lower) / (latest_upper - latest_lower + 1e-10), 0, 1))

    return position, width
