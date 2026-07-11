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
INCOMPLETE_REPAIR_MIN_CLOSE_DIFF = 0.01


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
    """截至 as_of 的最近一个交易日（周末 + 法定休市）。"""
    from trading_calendar import latest_trading_day

    return latest_trading_day(as_of)


def latest_completed_trade_date(as_of: datetime | None = None) -> date:
    """最近一个已有完整日 K 的交易日（09:30 开盘前决策用）。

    A 股 15:00 收盘；16:00 前认为「当日 K 线尚未落定」，目标回退到上一交易日。
    周一/节后开盘前运行时应接受 CSV 末日为上一交易日，而不是强求含「今天」。
    """
    from trading_calendar import is_trading_day, latest_trading_day

    now = as_of or datetime.now()
    d = now.date()
    if is_trading_day(d) and now.time() < dt_time(16, 0):
        d -= timedelta(days=1)
    return latest_trading_day(d)


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
    """Tushare fund_daily fallback — reliable when AKShare is blocked by campus network.

    Token 必须来自环境变量 TUSHARE_TOKEN，未设置时直接跳过（不再硬编码密钥，
    避免随代码库/GitHub 泄露）。当前主链路 fetch_etf_hist 默认不调用此函数，
    仅 experiment/multi-source-fallback 分支及手动脚本会用到。
    """
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token:
        return None
    try:
        import tushare as ts
    except ImportError:
        return None

    c = str(code).zfill(6)
    ts_code = f"{c}.SH" if c.startswith(("5", "6")) else f"{c}.SZ"

    try:
        pro = ts.pro_api(token)
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


def bar_row_looks_incomplete(row: pd.Series | dict[str, Any]) -> bool:
    """Detect half-baked daily bars (close stuck near open while range is wide).

    2026-07-10 AkShare returned 588000 close≈open (2.333/2.334) with a real
    high/low range; Baostock close was 2.209 (−5%). That corrupted settlement.
    """
    try:
        o = float(row["open"])
        c = float(row["close"])
        h = float(row["high"])
        low = float(row["low"])
    except Exception:
        return False
    if o <= 0:
        return False
    near_open = abs(c - o) / o < 0.005
    wide_range = (h - low) / o > 0.02
    return bool(near_open and wide_range)


def _last_bar_looks_incomplete(df: pd.DataFrame) -> bool:
    if df is None or len(df) < 1:
        return False
    return bar_row_looks_incomplete(df.iloc[-1])


def repair_incomplete_history(code: str, *, lookback: int = 40) -> dict[str, Any]:
    """Scan recent local CSV rows; replace incomplete bars from Baostock.

    Only the fetch last-bar check is not enough — older half-bars stay in CSV
    and poison ret_1d/ret_5d. Call after each successful update.
    """
    code = str(code).zfill(6)
    path = DATA_DIR / f"{code}.csv"
    result: dict[str, Any] = {"code": code, "repaired_dates": [], "ok": False}
    if not path.exists():
        return result
    try:
        local = pd.read_csv(path)
        col = "日期" if "日期" in local.columns else "date"
        local = local.rename(columns={
            col: "date", "开盘": "open", "最高": "high",
            "最低": "low", "收盘": "close", "成交量": "volume",
        })
        local["date"] = pd.to_datetime(local["date"], errors="coerce")
        for c in ("open", "high", "low", "close", "volume"):
            if c in local.columns:
                local[c] = pd.to_numeric(local[c], errors="coerce")
        local = local.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    except Exception:
        return result

    if local.empty:
        return result
    tail = local.tail(lookback)
    bad_idx = [int(i) for i in tail.index if bar_row_looks_incomplete(local.loc[i])]
    if not bad_idx:
        result["ok"] = True
        return result

    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=max(lookback * 2, 120))).strftime("%Y%m%d")
    bs = _fetch_baostock(code, start, end)
    if bs is None or bs.empty:
        return result

    bs = bs.copy()
    bs["date"] = pd.to_datetime(bs["date"], errors="coerce")
    bs_by_date = {d.normalize(): r for d, r in zip(bs["date"], bs.itertuples(index=False)) if pd.notna(d)}

    repaired: list[str] = []
    for i in bad_idx:
        d = pd.to_datetime(local.at[i, "date"]).normalize()
        src = bs_by_date.get(d)
        if src is None:
            continue
        try:
            src_open = float(src.open)
            src_high = float(src.high)
            src_low = float(src.low)
            src_close = float(src.close)
            local_close = float(local.at[i, "close"])
        except (TypeError, ValueError):
            continue
        if (
            min(src_open, src_high, src_low, src_close) <= 0
            or src_high < max(src_open, src_close)
            or src_low > min(src_open, src_close)
            or src_high < src_low
        ):
            continue
        close_diff = abs(src_close - local_close) / max(abs(src_close), 1e-9)
        if close_diff < INCOMPLETE_REPAIR_MIN_CLOSE_DIFF:
            # The shape heuristic also matches legitimate flat-close sessions.
            # Repair only when the independent source confirms a real mismatch.
            continue
        local.at[i, "open"] = src_open
        local.at[i, "high"] = src_high
        local.at[i, "low"] = src_low
        local.at[i, "close"] = src_close
        if hasattr(src, "volume") and src.volume is not None:
            local.at[i, "volume"] = float(src.volume)
        repaired.append(d.strftime("%Y-%m-%d"))

    if repaired:
        save_etf_csv(code, local, source="baostock")
    result["repaired_dates"] = repaired
    result["ok"] = True
    return result


def fetch_etf_hist(code: str, *, days: int = 800) -> tuple[pd.DataFrame | None, str]:
    """优先 AkShare；失败或末日K线异常时回退 Baostock（同为前复权）。

    2026-07 实测：东方财富/AkShare 连续多日 ConnectionError，或收盘后仍返回
    「收盘≈开盘」的半截K线。Baostock adjustflag=2 前复权与本地口径一致。
    Tushare fund_daily 仍不启用（不复权，会污染历史）。
    """
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    code = str(code).zfill(6)

    df = _fetch_akshare(code, start, end)
    if df is not None and not _last_bar_looks_incomplete(df):
        return df, "akshare"

    df_bs = _fetch_baostock(code, start, end)
    if df_bs is not None:
        return df_bs, "baostock"

    # 末日半截K且 Baostock 不可用时，宁可不更新也不写入可疑收盘价
    # （2026-07-10 AkShare 588000 close≈open 而真实收盘差 5%+）。
    if df is not None and not _last_bar_looks_incomplete(df):
        return df, "akshare"

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
    for col in ("open", "high", "low", "close", "volume"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    # Always merge into existing history when local file is longer / has older
    # dates — never let a short repair window truncate multi-year CSV.
    if path.exists():
        try:
            existing = pd.read_csv(path)
            col = "日期" if "日期" in existing.columns else "date"
            existing = existing.rename(columns={
                col: "date", "开盘": "open", "最高": "high",
                "最低": "low", "收盘": "close", "成交量": "volume",
            })
            existing["date"] = pd.to_datetime(existing["date"], errors="coerce")
            for c in ("open", "high", "low", "close", "volume"):
                if c in existing.columns:
                    existing[c] = pd.to_numeric(existing[c], errors="coerce")
            existing = existing.dropna(subset=["date"]).sort_values("date")

            if source not in QFQ_CONSISTENT_SOURCES:
                # Non-qfq sources: only append brand-new dates.
                new_dates_only = out[~out["date"].isin(existing["date"])]
                out = pd.concat([existing, new_dates_only], ignore_index=True)
            else:
                # Qfq-consistent: upsert by date, keep older local rows not in fetch.
                combined = pd.concat([existing, out], ignore_index=True)
                combined = combined.dropna(subset=["date", "close"])
                combined = combined.sort_values("date").drop_duplicates("date", keep="last")
                out = combined.reset_index(drop=True)
        except Exception:
            pass  # 本地文件损坏时退化为整份覆盖，保底可用

    out = out.sort_values("date").reset_index(drop=True)
    # Canonical column order used across the repo (AkShare-style).
    ordered_cols = ["date", "open", "close", "high", "low", "volume"]
    keep = [c for c in ordered_cols if c in out.columns]
    extras = [c for c in out.columns if c not in keep and c != "date"]
    out = out[keep + extras]
    out = out.rename(columns={
        "date": "日期", "open": "开盘", "high": "最高",
        "low": "最低", "close": "收盘", "volume": "成交量",
    })
    out.to_csv(path, index=False)
    return path


def update_one_etf(
    code: str,
    name: str = "",
    *,
    max_attempts: int = 3,
    stale_tolerance_days: int = 6,
) -> dict[str, Any]:
    """拉取并落盘；实时抓取新鲜即为完全成功。

    若实时抓取全部失败，退化为检查本地已有 CSV 是否仍在容忍窗口内
    （``stale_tolerance_days``，默认 6 天，覆盖「跨长假 + 数据源多日故障」的
    极端情况）——退化命中时仍返回 ``ok=True``，但标记 ``degraded=True``。

    设计动机：比赛按日提交，错过当天提交（无任何合规 JSON 输出）的代价
    远高于用稍旧数据做一次决策；本地数据来自上一次成功抓取，天然是
    「上一交易日或更早」的真实前复权行情，不存在准确性问题，只是不够新。
    """
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
        repair = repair_incomplete_history(code, lookback=40)
        return {
            "code": code,
            "name": name,
            "ok": True,
            "source": src,
            "last_date": str(df_last_date(df)),
            "degraded": False,
            "repaired_dates": repair.get("repaired_dates") or [],
        }

    existing_last = csv_last_date(code)
    if existing_last is not None and (target - existing_last).days <= stale_tolerance_days:
        repair = repair_incomplete_history(code, lookback=40)
        return {
            "code": code,
            "name": name,
            "ok": True,
            "source": "cached_stale",
            "last_date": str(existing_last),
            "error": last_err,
            "degraded": True,
            "repaired_dates": repair.get("repaired_dates") or [],
        }

    return {
        "code": code,
        "name": name,
        "ok": False,
        "source": "none",
        "last_date": str(existing_last or ""),
        "error": last_err,
        "degraded": True,
    }


def ensure_pool_fresh(
    codes: list[str],
    names: dict[str, str] | None = None,
    *,
    log_fn=None,
    min_ok_ratio: float = 0.87,
    min_usable_ratio: float = 0.25,
) -> tuple[list[dict], list[dict]]:
    """更新交易池行情。

    完全新鲜（当天实时抓取成功）比例低于 ``min_ok_ratio`` 只记警告，不中断；
    只有当「完全新鲜 + 缓存降级」合计可用比例低于 ``min_usable_ratio``
    （默认 25%，即数据源与本地缓存同时大范围失效，环境本身可能有问题）
    时才抛 RuntimeError 终止——这种情况下降级决策已无意义。
    返回 (成功列表[含降级], 失败列表)。
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
            tag = "OK(缓存降级)" if r.get("degraded") else "OK"
            _log(f"  [{i+1}/{len(codes)}] {code} {tag}  {r['source']}  last={r['last_date']}")
        else:
            fail_list.append(r)
            _log(f"  [{i+1}/{len(codes)}] {code} FAIL last={r['last_date']} ({r.get('error')})")
        pytime.sleep(1.2)

    usable_ratio = len(ok_list) / max(1, len(codes))
    fresh_ratio = sum(1 for r in ok_list if not r.get("degraded")) / max(1, len(codes))
    _log(
        f"  行情更新: 可用 {len(ok_list)}/{len(codes)} ({usable_ratio:.0%})，"
        f"其中完全新鲜 {fresh_ratio:.0%}"
    )

    if usable_ratio < min_usable_ratio:
        raise RuntimeError(
            f"行情数据严重不足：仅 {len(ok_list)}/{len(codes)} 只有可用数据"
            f"（含缓存降级），AkShare 与本地缓存同时大范围失效，无法生成预测。"
        )
    if fresh_ratio < min_ok_ratio:
        _log(
            f"  [WARN] 完全新鲜比例 {fresh_ratio:.0%} 低于目标 {min_ok_ratio:.0%}，"
            f"部分 ETF 使用缓存历史数据决策，预测仍会正常生成但置信度降低。"
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
                # 含当日行供新鲜度校验；决策侧 features 会再截成 date < date_str
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
