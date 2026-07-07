"""ETF 交易池定义、动态池切换规则与内存缓存层。

本模块从 strategy.py 拆出，集中管理：
  - TRADING_POOL / OFFENSIVE_POOL（固定交易池与动态进攻池）
  - 进攻池启用/退出阈值
  - Cache 类（通用 TTL 内存缓存）
  - get_stock_pool / _fetch_pool_raw（AkShare 动态池拉取，当前主策略未使用但保留兼容）

外部依赖方（通过 ``from pool import ...`` 引用）：
  - TRADING_POOL → daily_job, agent_kb, etf_agent_chat, news_fetcher, update_local_csv 等
  - OFFENSIVE_POOL → daily_job, agent_kb, etf_agent_chat, run_news_backtest
  - OFFENSIVE_ON_THRESHOLD → daily_job, run_news_backtest
"""

from __future__ import annotations

import os
import time
from datetime import datetime

# ================================================
# 固定交易池（8 只）
#
# 【结算口径更正说明，2026-07】平台实际按「昨收→今收」结算（买入价=前一
# 交易日收盘价，非当日开盘价，见 investment-daily-submit.html）。此前以
# 「开盘买→收盘卖」口径测算认为黄金/豆粕日内期望为负而移出交易池；
# 用正确口径复核后，85 日回测显示移出与否几乎打平（含黄金/豆粕 +6.31%
# vs 不含 +6.10%，两个子窗口互有胜负），并非确凿的收益改进。
# 保留当前 8 只精简配置，理由是分散度更集中、少一类与新闻信号弱相关
# 的商品资产，而非基于已证实的收益优势；后续样本变长后可重新验证。
# ================================================
TRADING_POOL: list[dict] = [
    # P1 宽基（主力，5 只）
    {"code": "510300", "name": "沪深300ETF",     "category": "全市场"},
    {"code": "510050", "name": "上证50ETF",      "category": "大盘蓝筹"},
    {"code": "510500", "name": "中证500ETF",     "category": "中盘成长"},
    {"code": "510330", "name": "华夏沪深300ETF", "category": "全市场"},
    {"code": "159338", "name": "中证A500ETF",    "category": "全市场"},
    # P2 红利防御
    {"code": "510880", "name": "红利ETF",        "category": "高股息防御"},
    # P3 行业（仅保留流动性大且不极端的）
    {"code": "512880", "name": "证券ETF",        "category": "券商周期"},
    {"code": "512010", "name": "医药ETF",        "category": "医疗"},
]

# 动态进攻池：仅当宽基复合趋势分 ≥ +3% 时临时启用，搭强势顺风车。
# 科创50/创业板50 虽是全池开→收日内漂移最强的标的（近250日约 +0.35%/日），
# 但 2026-03~04 的 43 日全链路回测实测：阈值降到 +1%/+2% 收益反而从
# +2.61% 降至 +1.42%/+2.37%——震荡市里成长票入池是减分项，维持 +3%。
OFFENSIVE_POOL: list[dict] = [
    {"code": "159915", "name": "创业板ETF",  "category": "创业板"},
    {"code": "588000", "name": "科创50ETF",  "category": "科创板"},
    {"code": "159949", "name": "创业板50ETF", "category": "创蓝筹"},
]
OFFENSIVE_ON_THRESHOLD: float = 3.0   # 宽基复合趋势分 ≥ +3% 加进攻池
OFFENSIVE_OFF_THRESHOLD: float = 1.0  # 预留退出滞回阈值（每日独立评估，当前未参与判断）


# ================================================
# 缓存层 (Memory Cache)
# ================================================

class Cache:
    """简单内存缓存，支持 TTL 过期。"""

    def __init__(self, ttl_seconds: int = 300):
        self._data: dict[str, object] = {}
        self._timestamps: dict[str, float] = {}
        self.ttl = ttl_seconds
        self.cache_dir = os.path.join(os.path.dirname(__file__), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    def _key(self, *args: object) -> str:
        return "|".join(str(a) for a in args)

    def _is_valid(self, key: str) -> bool:
        if key not in self._data:
            return False
        return (time.time() - self._timestamps.get(key, 0)) < self.ttl

    def get(self, *args: object):
        key = self._key(*args)
        return self._data[key] if self._is_valid(key) else None

    def set(self, value: object, *args: object) -> None:
        key = self._key(*args)
        self._data[key] = value
        self._timestamps[key] = time.time()

    def get_or_fetch(self, fetch_fn, *args):
        key = self._key(*args)
        if self._is_valid(key):
            return self._data[key]
        value = fetch_fn()
        if value is not None:
            self._data[key] = value
            self._timestamps[key] = time.time()
        return value

    def invalidate(self, *args: object) -> None:
        key = self._key(*args)
        self._data.pop(key, None)
        self._timestamps.pop(key, None)

    def get_or_compute(self, compute_fn, *args):
        key = self._key(*args)
        return self._data.get(key)


# 全局缓存实例
_pool_cache = Cache(ttl_seconds=300)   # 5分钟缓存
_price_cache = Cache(ttl_seconds=86400) # 盘中当日数据不过期


# ================================================
# 动态池拉取（当前主策略未使用，保留兼容）
# ================================================

def _fetch_pool_raw(max_stocks: int) -> list[dict]:
    """从 AkShare 拉取动态候选池（ETF + 涨停 + 概念板块）。"""
    import akshare as ak

    pool: list[dict] = []
    today = datetime.now().strftime("%Y-%m-%d")

    # ETF 基准池（成交额最高的 8 只）
    try:
        etf_df = ak.fund_etf_spot_em()
        if etf_df is not None and len(etf_df) > 0:
            amount_col = [c for c in etf_df.columns if "成交额" in c or "amount" in c.lower()]
            if amount_col:
                top = etf_df.nlargest(8, amount_col[0])
            else:
                top = etf_df.head(8)
            code_col = [c for c in etf_df.columns if "code" in c.lower() or "代码" in c][0]
            name_col = [c for c in etf_df.columns if "name" in c.lower() or "名称" in c][0]
            for _, row in top.iterrows():
                code = str(row[code_col]).zfill(6)
                pool.append({"code": code, "name": row[name_col], "source": "etf_top"})
    except Exception as e:
        print(f"[Pool-ETF] {e}")

    # 涨停强势股
    try:
        df_zt = ak.stock_zt_pool_strong_em()
        if df_zt is not None and len(df_zt) > 0:
            code_col = [c for c in df_zt.columns if "code" in c.lower() or "代码" in c][0]
            name_col = [c for c in df_zt.columns if "name" in c.lower() or "名称" in c][0]
            for _, row in df_zt.head(8).iterrows():
                code = str(row[code_col]).zfill(6)
                if code not in [p["code"] for p in pool]:
                    pool.append({"code": code, "name": row[name_col], "source": "limit_up"})
    except Exception as e:
        print(f"[Pool-ZT] {e}")

    # 热门概念板块龙头
    try:
        df_board = ak.stock_board_concept_name_em()
        if df_board is not None and len(df_board) > 0:
            amount_col = [c for c in df_board.columns if "成交额" in c or "amount" in c.lower()]
            if amount_col:
                top_boards = df_board.nlargest(2, amount_col[0])
            else:
                top_boards = df_board.head(2)
            name_col_b = [c for c in df_board.columns
                          if "板块名称" in c or "概念名称" in c or c == "名称"][0]
            for _, board in top_boards.iterrows():
                bname = board[name_col_b]
                try:
                    cons = ak.stock_board_concept_cons_em(symbol=bname)
                    if cons is not None:
                        cc = [c for c in cons.columns if "code" in c.lower() or "代码" in c][0]
                        nc = [c for c in cons.columns if "name" in c.lower() or "名称" in c][0]
                        for _, s in cons.head(3).iterrows():
                            code = str(s[cc]).zfill(6)
                            if code not in [p["code"] for p in pool]:
                                pool.append({"code": code, "name": s[nc], "source": f"concept:{bname}"})
                except Exception:
                    pass
    except Exception as e:
        print(f"[Pool-Board] {e}")

    # 去重
    seen: set[str] = set()
    unique: list[dict] = []
    for p in pool:
        if p["code"] not in seen:
            seen.add(p["code"])
            unique.append(p)
    return unique[:max_stocks]


def get_stock_pool(max_stocks: int = 15) -> list[dict]:
    """获取候选股票池（含 5 分钟缓存）。"""
    cached = _pool_cache.get("pool", max_stocks)
    if cached is not None:
        print(f"[Pool] Cache hit ({len(cached)} stocks)")
        return cached

    print(f"[Pool] Cache miss - fetching pool (max={max_stocks})...")
    pool = _fetch_pool_raw(max_stocks)
    _pool_cache.set(pool, "pool", max_stocks)
    print(f"[Pool] Fetched {len(pool)} stocks, cached for 5 min")
    return pool


def get_trading_pool():
    """
    获取交易池：核心ETF + 卫星ETF
    分为核心（3只压舱石，永远持有）和卫星（其余）
    """
    core_codes = {c["code"] for c in [
        {"code": "510880", "name": "红利ETF",   "allocation": 0.10},
        {"code": "510050", "name": "上证50ETF", "allocation": 0.10},
        {"code": "510300", "name": "沪深300ETF","allocation": 0.10},
    ]}
    all_etfs = [dict(item) for item in TRADING_POOL]
    core = [e for e in all_etfs if e["code"] in core_codes]
    satellite = [e for e in all_etfs if e["code"] not in core_codes]
    return core, satellite
