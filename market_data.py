"""ETF 行情拉取：多源回退 + 禁用系统代理 + 新鲜度校验。"""
from __future__ import annotations

import os
import time as pytime
from contextlib import contextmanager
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 允许比「最近已完成交易日」落后几天仍算可用（周末/小长假后首日等）
DEFAULT_MAX_LAG_DAYS = 3


@contextmanager
def _no_proxy_env():
    """AkShare/requests 常被错误系统代理打断，临时清掉代理环境变量。"""
    keys = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "all_proxy",
    )
    saved = {k: os.environ[k] for k in keys if k in os.environ}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.setdefault("NO_PROXY", "*")
    try:
        yield
    finally:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in saved.items():
            os.environ[k] = v


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        "日期": "date", "date": "date",
        "开盘": "open", "open": "open",
        "收盘": "close", "close": "close",
        "最高": "high", "high": "high",
        "最低": "low", "low": "low",
        "成交量": "volume", "volume": "volume",
    }
    out = df.rename(columns={c: rename[c] for c in df.columns if c in rename})
    if "date" not in out.columns:
        return out
    out["date"] = pd.to_datetime(out["date"])
    for c in ("open", "high", "low", "close", "volume"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.sort_values("date").reset_index(drop=True)


def latest_trade_date(as_of: date | None = None) -> date:
    """截至 as_of 的最近一个交易日（跳过周末）。"""
    d = as_of or datetime.now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def latest_completed_trade_date(as_of: datetime | None = None) -> date:
    """最近一个已有完整日 K 的交易日（09:30 开盘前决策用）。

    A 股 15:00 收盘；16:00 前认为「当日 K 线尚未落定」，目标回退到上一交易日。
    周一 09:25 运行时应接受 CSV 末日为上周五，而不是强求含「今天」。
    """
    now = as_of or datetime.now()
    d = now.date()
    if d.weekday() < 5 and now.time() < dt_time(16, 0):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _csv_date_column(path: Path) -> str:
    header = pd.read_csv(path, nrows=0).columns
    return "日期" if "日期" in header else "date"


def csv_date_range(code: str) -> tuple[date | None, date | None, int]:
    """返回 (首日, 末日, 行数)。"""
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None, None, 0
    try:
        col = _csv_date_column(path)
        df = pd.read_csv(path, usecols=[col])
        if len(df) == 0:
            return None, None, 0
        ser = pd.to_datetime(df[col], errors="coerce").dropna()
        if ser.empty:
            return None, None, 0
        return ser.iloc[0].date(), ser.iloc[-1].date(), int(len(ser))
    except Exception:
        return None, None, 0


def csv_last_date(code: str) -> date | None:
    _, last, _ = csv_date_range(code)
    return last


def df_last_date(df: pd.DataFrame | None) -> date | None:
    if df is None or len(df) == 0:
        return None
    if "date" not in df.columns:
        return None
    return pd.to_datetime(df["date"].iloc[-1]).date()


def _ref_trade_dates() -> pd.Series:
    """参考 510300 本地 CSV 的交易日序列。"""
    for ref_code in ("510300", "510500", "159915"):
        first, last, n = csv_date_range(ref_code)
        if n < 30:
            continue
        path = DATA_DIR / f"{ref_code}.csv"
        col = _csv_date_column(path)
        df = pd.read_csv(path, usecols=[col])
        return pd.to_datetime(df[col], errors="coerce").dropna().sort_values()
    return pd.Series(dtype="datetime64[ns]")


def resolve_sim_date_bounds(
    start: str | None = None,
    end: str | None = None,
    days: int = 10,
) -> tuple[date, date]:
    """根据回测参数与本地 CSV 交易日，确定需要的覆盖区间。"""
    trade_dates = _ref_trade_dates()
    if len(trade_dates) == 0:
        if end:
            sim_end = pd.to_datetime(end).date()
        else:
            sim_end = latest_trade_date()
        if start:
            sim_start = pd.to_datetime(start).date()
        else:
            sim_start = sim_end - timedelta(days=int(max(days, 10) * 1.5))
        return sim_start, sim_end

    if start and end:
        s = pd.to_datetime(start)
        e = pd.to_datetime(end)
        window = trade_dates[(trade_dates >= s) & (trade_dates <= e)]
    elif end:
        e = pd.to_datetime(end)
        window = trade_dates[trade_dates <= e].tail(max(days, 2))
    else:
        window = trade_dates.tail(max(days, 2))

    if len(window) < 2:
        sim_end = latest_trade_date()
        sim_start = sim_end - timedelta(days=int(max(days, 10) * 1.5))
        return sim_start, sim_end

    return window.iloc[0].date(), window.iloc[-1].date()


def check_pool_csv_ready(
    codes: list[str],
    *,
    sim_start: date,
    sim_end: date,
    min_rows: int = 50,
    max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
    lookback_buffer_days: int = 30,
) -> tuple[bool, str, list[str]]:
    """
    检查 data/*.csv 是否已覆盖回测区间。
    返回 (是否齐全, 摘要说明, 缺失/过期的代码列表)。
    """
    target_end = latest_trade_date(sim_end)
    need_from = sim_start - timedelta(days=lookback_buffer_days)
    issues: list[str] = []

    for code in codes:
        code = str(code).zfill(6)
        first, last, n = csv_date_range(code)
        if first is None or last is None or n < min_rows:
            issues.append(f"{code}(无文件或不足{min_rows}行)")
            continue
        if last < target_end - timedelta(days=max_lag_days):
            issues.append(f"{code}(末行{last} 旧于{target_end})")
        if first > need_from:
            issues.append(f"{code}(首行{first} 晚于{need_from})")

    if not issues:
        msg = (
            f"本地 CSV 已覆盖 {sim_start}~{sim_end} "
            f"(齐全、≥{min_rows}行、末日≥{target_end})"
        )
        return True, msg, []

    return False, f"需同步: {len(issues)} 项 — " + "; ".join(issues[:5]), issues


def is_fresh(
    df: pd.DataFrame | None,
    *,
    max_lag_days: int = DEFAULT_MAX_LAG_DAYS,
    as_of: date | datetime | None = None,
) -> bool:
    last = df_last_date(df)
    if last is None:
        return False
    if isinstance(as_of, datetime):
        target = latest_completed_trade_date(as_of)
    elif isinstance(as_of, date):
        target = latest_completed_trade_date(datetime.combine(as_of, dt_time(16, 0)))
    else:
        target = latest_completed_trade_date()
    return (target - last).days <= max_lag_days


def _bs_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("5", "6")):
        return f"sh.{code}"
    return f"sz.{code}"


def _fetch_akshare(code: str, start: str, end: str) -> pd.DataFrame | None:
    import akshare as ak

    with _no_proxy_env():
        for attempt in range(4):
            try:
                df = ak.fund_etf_hist_em(
                    symbol=code, period="daily",
                    start_date=start, end_date=end, adjust="qfq",
                )
                if df is not None and len(df) >= 20:
                    return _normalize_df(df)
            except Exception:
                pytime.sleep(1.0 + attempt * 0.8)
        for attempt in range(2):
            try:
                df = ak.stock_zh_a_hist(
                    symbol=code, period="daily",
                    start_date=start, end_date=end, adjust="qfq",
                )
                if df is not None and len(df) >= 20:
                    return _normalize_df(df)
            except Exception:
                pytime.sleep(1.0)
    return None


def _fetch_baostock(code: str, start: str, end: str) -> pd.DataFrame | None:
    try:
        import baostock as bs
    except ImportError:
        return None

    sym = _bs_symbol(code)
    start_d = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    end_d = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

    with _no_proxy_env():
        try:
            lg = bs.login()
            if lg.error_code != "0":
                return None
            rs = bs.query_history_k_data_plus(
                sym,
                "date,open,high,low,close,volume",
                start_date=start_d,
                end_date=end_d,
                frequency="d",
                adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            bs.logout()
            if len(rows) < 20:
                return None
            df = pd.DataFrame(rows, columns=rs.fields)
            return _normalize_df(df)
        except Exception:
            try:
                bs.logout()
            except Exception:
                pass
    return None


def _fetch_yfinance(code: str, days: int = 900) -> pd.DataFrame | None:
    try:
        import yfinance as yf
    except ImportError:
        return None

    code = str(code).zfill(6)
    if code.startswith("5") or code.startswith("6"):
        ticker = f"{code}.SS"
    else:
        ticker = f"{code}.SZ"

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)

    with _no_proxy_env():
        try:
            yf_df = yf.download(
                ticker,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                progress=False,
                auto_adjust=True,
            )
            if yf_df is None or len(yf_df) < 20:
                return None
            if hasattr(yf_df.columns, "levels"):
                yf_df.columns = yf_df.columns.droplevel(1)
            df = pd.DataFrame({
                "date": pd.to_datetime(yf_df.index),
                "open": yf_df["Open"].values,
                "high": yf_df["High"].values,
                "low": yf_df["Low"].values,
                "close": yf_df["Close"].values,
                "volume": yf_df["Volume"].values,
            })
            return _normalize_df(df.dropna())
        except Exception:
            return None


def _fetch_tushare(code: str, start: str, end: str) -> pd.DataFrame | None:
    """Tushare fund_daily fallback — reliable when AKShare is blocked by campus network."""
    try:
        import tushare as ts
    except ImportError:
        return None

    c = str(code).zfill(6)
    ts_code = f"{c}.SH" if c.startswith(("5", "6")) else f"{c}.SZ"

    try:
        pro = ts.pro_api("51a6abcf6ea12364b1a78f5c782c1058ba4e9839f6cb43853e8ca1da")
        s = pd.to_datetime(start).strftime("%Y%m%d")
        e = pd.to_datetime(end).strftime("%Y%m%d")
        df = pro.fund_daily(ts_code=ts_code, start_date=s, end_date=e,
                           fields="trade_date,open,high,low,close,vol,amount")
        if df is None or len(df) < 20:
            return None
        df = df.rename(columns={
            "trade_date": "date", "open": "open", "high": "high",
            "low": "low", "close": "close", "vol": "volume",
        })
        return _normalize_df(df)
    except Exception:
        return None


def fetch_etf_hist(code: str, *, days: int = 800) -> tuple[pd.DataFrame | None, str]:
    """
    按顺序尝试：AkShare → Tushare → Baostock → yfinance。
    返回 (df, source_name)。
    """
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    code = str(code).zfill(6)

    df = _fetch_akshare(code, start, end)
    if df is not None:
        return df, "akshare"

    df = _fetch_tushare(code, start, end)
    if df is not None:
        return df, "tushare"

    df = _fetch_baostock(code, start, end)
    if df is not None:
        return df, "baostock"

    df = _fetch_yfinance(code, days=days)
    if df is not None:
        return df, "yfinance"

    return None, "none"


# 仅 AkShare/Baostock 使用前复权(qfq)；Tushare fund_daily 返回不复权原始价，
# 与本地历史 CSV 的复权口径不一致。若整份覆盖，会把已经正确前复权的历史
# 行情全部替换成不复权数据，导致跨越除息日的多日动量/趋势特征失真——
# 510880(红利ETF)这类高股息标的尤其敏感。故非 qfq 一致来源只做增量补天，
# 不触碰已有历史行。
QFQ_CONSISTENT_SOURCES = {"akshare", "baostock", "yfinance"}


def save_etf_csv(code: str, df: pd.DataFrame, *, source: str = "akshare") -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"])

    if source not in QFQ_CONSISTENT_SOURCES and path.exists():
        try:
            existing = pd.read_csv(path)
            col = "日期" if "日期" in existing.columns else "date"
            existing = existing.rename(columns={
                col: "date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
            })
            existing["date"] = pd.to_datetime(existing["date"])
            new_dates_only = out[~out["date"].isin(existing["date"])]
            out = pd.concat([existing, new_dates_only], ignore_index=True)
            out = out.sort_values("date").reset_index(drop=True)
        except Exception:
            pass  # 本地文件损坏时退化为整份覆盖，保底可用

    out = out.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高",
        "low": "最低", "close": "收盘", "volume": "成交量",
    })
    out.to_csv(path, index=False)
    return path


def update_one_etf(code: str, name: str = "", *, max_attempts: int = 3) -> dict[str, Any]:
    """拉取并落盘；必须新鲜才返回 success=True。"""
    target = latest_completed_trade_date()
    last_err = ""
    for attempt in range(max_attempts):
        df, src = fetch_etf_hist(code)
        if df is None:
            last_err = "all_sources_failed"
            pytime.sleep(2 + attempt)
            continue
        if not is_fresh(df, as_of=target):
            last_err = f"stale_last={df_last_date(df)} need>={target}"
            pytime.sleep(1.5)
            continue
        save_etf_csv(code, df, source=src)
        return {
            "code": code,
            "name": name,
            "ok": True,
            "source": src,
            "last_date": str(df_last_date(df)),
        }
    return {
        "code": code,
        "name": name,
        "ok": False,
        "source": "none",
        "last_date": str(csv_last_date(code) or ""),
        "error": last_err,
    }


def ensure_pool_fresh(
    codes: list[str],
    names: dict[str, str] | None = None,
    *,
    log_fn=None,
    min_ok_ratio: float = 0.87,
) -> tuple[list[dict], list[dict]]:
    """
    更新交易池行情；至少 min_ok_ratio 比例必须新鲜。
    返回 (成功列表, 失败列表)。失败过多时抛 RuntimeError。
    """
    names = names or {}
    ok_list: list[dict] = []
    fail_list: list[dict] = []

    def _log(m: str) -> None:
        if log_fn:
            log_fn(m)

    target = latest_completed_trade_date()
    _log(f"  目标最近交易日: {target}")

    for i, code in enumerate(codes):
        code = str(code).zfill(6)
        r = update_one_etf(code, names.get(code, ""), max_attempts=4)
        if r["ok"]:
            ok_list.append(r)
            _log(f"  [{i+1}/{len(codes)}] {code} OK  {r['source']}  last={r['last_date']}")
        else:
            fail_list.append(r)
            _log(f"  [{i+1}/{len(codes)}] {code} FAIL last={r['last_date']} ({r.get('error')})")
        pytime.sleep(1.2)

    ratio = len(ok_list) / max(1, len(codes))
    _log(f"  行情更新: 成功 {len(ok_list)}/{len(codes)} ({ratio:.0%})")

    if ratio < min_ok_ratio:
        raise RuntimeError(
            f"行情数据不足：仅 {len(ok_list)}/{len(codes)} 只更新到 {target}，"
            f"所有数据源均失败 (akshare/tushare/baostock/yfinance)。"
        )
    return ok_list, fail_list


def _read_csv(code: str, date_str: str | None = None) -> pd.DataFrame | None:
    path = DATA_DIR / f"{str(code).zfill(6)}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        if "日期" in df.columns:
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
            })
        df["date"] = pd.to_datetime(df["date"])
        if date_str:
            cutoff = pd.to_datetime(date_str, errors="coerce")
            if pd.notna(cutoff):
                df = df[df["date"] <= cutoff]
        return df.reset_index(drop=True) if len(df) >= 20 else None
    except Exception:
        return None


def load_fresh_price(code: str, date_str: str | None = None) -> pd.DataFrame | None:
    """
    实盘（date=今天）：必须新鲜，否则在线刷新；仍陈旧则返回 None（不用旧数据）。
    回测（date<今天）：只读本地 CSV 截断到 date，不强制联网刷新。
    """
    code = str(code).zfill(6)
    as_of = pd.to_datetime(date_str).date() if date_str else datetime.now().date()
    today = datetime.now().date()
    target = latest_trade_date(as_of)
    hist_mode = as_of < today

    df = _read_csv(code, date_str)
    if hist_mode:
        if df is None:
            return None
        last = df_last_date(df)
        if last is None or last < target - timedelta(days=3):
            return None
        return df

    if is_fresh(df, as_of=target):
        return df

    r = update_one_etf(code, max_attempts=4)
    if not r["ok"]:
        return None
    return _read_csv(code, date_str)
