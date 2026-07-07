"""Economic-calendar layer for the LLM-driven daily decision.

Fresh post-close news ranks above the calendar in the decision prompt; this
module supplies calendar context to the LLM and **hard position caps** in the
rule layer.  The calendar is deterministic (events known before the open) and
triggers exposure limits on high-impact data days (CPI, PMI, LPR, FOMC, ...),
regardless of LLM sentiment.

This module exposes one function the rest of the system uses:

    load_econ_payload(date_str, *, allow_live=True, lookback_hours=24)
        -> dict with structured events + 'has_high_impact_event' flag.

Data sources, in priority order:
  1. ``data/econ_calendar_cache/{date}.json`` (replay-friendly cache).
  2. AKShare ``news_economic_baidu`` (live; only when allow_live=True).
  3. SQLite ``baidu_economic`` channel summary articles (best-effort fallback).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "data" / "econ_calendar_cache"

# "重要性" >= HIGH_IMPORTANCE_THRESHOLD 触发硬规则（仓位上限 / LLM 高优先级）。
HIGH_IMPORTANCE_THRESHOLD = 3
MEDIUM_IMPORTANCE_THRESHOLD = 2

# 高影响事件关键词（用于 _line_to_event 中 sqlite 回退解析）
HIGH_IMPACT_EVENT_KEYWORDS = (
    "CPI", "PPI", "PMI", "LPR", "GDP", "非农", "失业率", "社融", "M2",
    "议息", "FOMC", "利率决议", "中央经济工作", "政治局会议",
    "降准", "降息", "MLF",
)


def _norm_date(date_str: str) -> str:
    return str(date_str)[:10]


def _ymd(date_str: str) -> str:
    return _norm_date(date_str).replace("-", "")


def _cache_path(date_str: str) -> Path:
    return CACHE_DIR / f"{_norm_date(date_str)}.json"


def _read_cache(date_str: str) -> dict[str, Any] | None:
    path = _cache_path(date_str)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(date_str: str, payload: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(date_str)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _importance_int(raw: Any) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


# 对 A 股有实际影响的国家/地区（其他地区的事件降级为低重要度）
A_SHARE_IMPACT_REGIONS = (
    "中国", "中国大陆", "中国香港",
    "美国",
    "欧元区", "欧盟",
    "日本", "韩国", "英国",
    "德国", "法国",  # 欧洲核心经济体
    "G20", "OECD", "OPEC",  # 国际组织
)

# 次级影响地区（仅 importance>=3 或 FOMC/利率决议等全球性事件才算高影响）


def _event_high_impact(event: dict[str, Any]) -> bool:
    """判断事件是否为高影响（严格地区过滤+阈值收紧）。

    规则：
    1. A股直接影响地区（中美欧日韩英）：importance>=2 + 关键词命中，或 importance>=3
    2. 次级地区（澳加瑞等）：仅 importance>=3 的顶级事件算高影响
    3. 其他地区（印尼/马来/南非等）：一律不算高影响
    4. 全球性事件（FOMC/非农/OPEC等）不论地区都算
    """
    importance = _importance_int(event.get("importance"))
    name = str(event.get("event") or "")
    region = str(event.get("region") or "")

    # 顶级重要性 >= 3：A股核心地区直接算，其他地区需是全球性事件
    if importance >= HIGH_IMPORTANCE_THRESHOLD:
        if any(r in region for r in A_SHARE_IMPACT_REGIONS):
            return True
        # 非核心地区但 importance==3 时，仅全球性事件算
        global_keywords = ("FOMC", "非农", "利率决议", "OPEC", "G20峰会")
        return any(k in name for k in global_keywords)

    # importance == 2：仅A股核心地区 + 关键词命中
    if importance >= MEDIUM_IMPORTANCE_THRESHOLD:
        if any(r in region for r in A_SHARE_IMPACT_REGIONS):
            # 必须是实质性宏观数据/政策事件
            a_share_core_keywords = (
                "CPI", "PPI", "PMI", "LPR", "GDP", "M2", "社融",
                "MLF", "降准", "降息", "议息", "FOMC", "利率决议",
                "非农", "失业率", "政治局会议", "中央经济工作", "国常会",
                "贸易帐", "进出口", "零售销售", "工业产出", "耐用品订单",
                "JOLTs", "消费者信心", "商业库存", "新屋开工",
                "制造业订单", "工厂订单",
            )
            # 排除日常更新类事件（每日仓单/持仓/库存变动等）
            routine_hints = ("每日", "每日更新", "日更", "仓单", "持仓变动")
            if any(h in name for h in routine_hints):
                return False
            return any(k in name for k in a_share_core_keywords)
        # 次级地区 importance==2：不算高影响（如瑞士GDP对A股影响有限）
        return False

    return False


def _row_to_event(row: pd.Series, *, default_date: str) -> dict[str, Any] | None:
    name = str(row.get("事件") or "").strip()
    if not name:
        return None
    return {
        "date": default_date,
        "time": str(row.get("时间") or "").strip(),
        "region": str(row.get("地区") or "").strip(),
        "event": name,
        "importance": _importance_int(row.get("重要性")),
        "expected": _stringify(row.get("预期")),
        "actual": _stringify(row.get("公布")),
        "previous": _stringify(row.get("前值")),
    }


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none", "null"}:
        return ""
    return text


def _fetch_live_one_day(date_str: str) -> list[dict[str, Any]]:
    """Pull one day of calendar from akshare.  Returns [] on any failure.

    优先使用 akshare 接口；若 curl_cffi SSL 错误则回退到直接 HTTP 请求。
    """
    iso_date = _norm_date(date_str)
    ymd = _ymd(date_str)
    events = _fetch_live_akshare(date_str, ymd, iso_date)
    if events:
        return events
    # akshare 失败时用备用直接 HTTP 方案
    events = _fetch_live_direct(date_str, ymd, iso_date)
    return events


def _fetch_live_akshare(date_str: str, ymd: str, iso_date: str) -> list[dict[str, Any]]:
    """通过 akshare 获取经济日历。"""
    try:
        import akshare as ak
        from market_data import _no_proxy_env
    except Exception:
        return []
    try:
        with _no_proxy_env():
            df = ak.news_economic_baidu(date=ymd)
    except Exception as exc:
        err_msg = str(exc)
        if "curl" in err_msg.lower() or "certificate" in err_msg.lower() or "CAfile" in err_msg.lower():
            print(f"[econ_calendar] akshare SSL error, falling back to direct HTTP...")
        else:
            print(f"[econ_calendar] akshare {date_str} failed: {exc}")
        return []
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    out: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        event = _row_to_event(row, default_date=iso_date)
        if event:
            out.append(event)
    return out


def _fetch_live_direct(date_str: str, ymd: str, iso_date: str) -> list[dict[str, Any]]:
    """直接 HTTP 请求百度经济日历 API（绕过 curl_cffi SSL 问题）。

    curl_cffi 编译时硬编码的 CAfile 路径在当前环境下无效，
    改用标准库 urllib + ssl 禁用验证作为兜底。
    """
    import json as _json
    import ssl
    import urllib.request
    import re as _re

    try:
        # 获取百度 Cookie
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        headers = {
            "accept": "application/vnd.finance-web.v1+json",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en,zh-CN;q=0.9,zh;q=0.8",
            "origin": "https://finance.baidu.com",
            "referer": "https://finance.baidu.com/",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
        }

        # Step 1: 获取 BAIDUID cookie
        req1 = urllib.request.Request(
            "https://finance.baidu.com/calendar", headers=headers, method="GET",
        )
        cookies = {}
        with urllib.request.urlopen(req1, timeout=15, context=ssl_ctx) as resp1:
            for cookie_header in resp1.headers.get_all("Set-Cookie") or []:
                match = _re.match(r"(\w+)=(\w+)", cookie_header)
                if match:
                    cookies[match.group(1)] = match.group(2)

        if "BAIDUID" not in cookies:
            print("[econ_calendar] direct HTTP: failed to get BAIDUID cookie")
            return []

        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers["cookie"] = cookie_str

        # Step 2: 请求经济日历 API
        formatted_date = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        url = (
            f"https://finance.pae.baidu.com/sapi/v1/financecalendar"
            f"?start_date={formatted_date}&end_date={formatted_date}"
            f"&pn=0&rn=100&cate=economic_data&finClientType=pc"
        )
        req2 = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req2, timeout=15, context=ssl_ctx) as resp2:
            data = _json.loads(resp2.read().decode("utf-8", errors="replace"))

        events: list[dict[str, Any]] = []
        result_obj = data.get("Result", {})
        calendar_info = result_obj.get("calendarInfo", [])
        for item in calendar_info:
            if item.get("date") == formatted_date and item.get("list"):
                for raw_event in item["list"]:
                    evt_name = str(raw_event.get("title", "")).strip()
                    if not evt_name:
                        continue
                    importance = int(raw_event.get("star", 0) or 0)
                    events.append({
                        "date": iso_date,
                        "time": str(raw_event.get("time", "")).strip(),
                        "region": str(raw_event.get("region", "")).strip(),
                        "event": evt_name,
                        "importance": importance,
                        "expected": _stringify(raw_event.get("formerVal", "")),
                        "actual": _stringify(raw_event.get("pubVal", "")),
                        "previous": _stringify(raw_event.get("formerVal", "")),
                    })

        if events:
            print(f"[econ_calendar] direct HTTP fallback got {len(events)} events for {date_str}")
        return events

    except Exception as exc:
        print(f"[econ_calendar] direct HTTP fallback failed: {exc}")
        return []


# 解析 news_aux_fetcher 写入 SQLite 时拼出的 " | " 分隔行。
_LINE_PATTERN = re.compile(
    r"^(?P<region>\S+?)?\s?(?P<event>.+?)\s+"
    r"(公布(?P<actual>[^\s/]+))?\s*/?\s*"
    r"(预期(?P<expected>[^\s/]+))?\s*/?\s*"
    r"(前值(?P<previous>.+))?$"
)


def _parse_sqlite_summary(date_str: str) -> list[dict[str, Any]]:
    try:
        from news_store import query_articles_before
    except Exception:
        return []
    try:
        cutoff = "23:59"
        articles = query_articles_before(
            date_str,
            cutoff_time=cutoff,
            lookback_hours=36,
            channels={"baidu_economic"},
        )
    except Exception as exc:
        print(f"[econ_calendar] SQLite read failed: {exc}")
        return []
    iso_date = _norm_date(date_str)
    out: list[dict[str, Any]] = []
    for art in articles:
        content = str(art.get("content") or "")
        if not content:
            continue
        for raw_line in content.split(" | "):
            line = raw_line.strip()
            if not line:
                continue
            event = _line_to_event(line, default_date=iso_date)
            if event:
                out.append(event)
    return out


def _line_to_event(line: str, *, default_date: str) -> dict[str, Any] | None:
    # 朴素解析："中国 5月LPR 公布3.10 / 预期3.10 / 前值3.10"
    parts = line.split()
    if not parts:
        return None
    region = ""
    name_parts: list[str] = []
    extras: dict[str, str] = {}
    for token in parts:
        if token.startswith("公布"):
            extras["actual"] = token[2:]
        elif token.startswith("预期"):
            extras["expected"] = token[2:]
        elif token.startswith("前值"):
            extras["previous"] = token[2:]
        elif "/" == token:
            continue
        elif not region and len(token) <= 6 and not any(c.isdigit() for c in token):
            region = token
        else:
            name_parts.append(token)
    event_name = " ".join(name_parts).strip()
    if not event_name:
        return None
    return {
        "date": default_date,
        "time": "",
        "region": region,
        "event": event_name,
        "importance": 2 if any(k in event_name for k in HIGH_IMPACT_EVENT_KEYWORDS) else 1,
        "expected": extras.get("expected", "").strip(",/"),
        "actual": extras.get("actual", "").strip(",/"),
        "previous": extras.get("previous", "").strip(",/"),
    }


def _dedupe(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        key = (ev.get("date", ""), ev.get("region", ""), ev.get("event", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def _sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda e: (-_importance_int(e.get("importance")), e.get("date", ""), e.get("time", "")),
    )


def load_econ_payload(
    date_str: str,
    *,
    allow_live: bool = True,
    lookback_days: int = 1,
    refresh: bool = False,
) -> dict[str, Any]:
    """Return the economic-calendar payload for the given trade date.

    The payload includes events scheduled for ``date_str`` plus the previous
    ``lookback_days`` days (so the LLM also sees yesterday's prints that may
    still drive today's open).
    """
    iso_date = _norm_date(date_str)

    if not refresh:
        cached = _read_cache(iso_date)
        if cached:
            return cached

    target_dates = [
        (datetime.strptime(iso_date, "%Y-%m-%d") - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(lookback_days, -1, -1)
    ]

    events: list[dict[str, Any]] = []
    source_used: list[str] = []

    if allow_live:
        for d in target_dates:
            day_events = _fetch_live_one_day(d)
            if day_events:
                events.extend(day_events)
        if events:
            source_used.append("akshare_live")

    if not events:
        for d in target_dates:
            day_events = _parse_sqlite_summary(d)
            if day_events:
                events.extend(day_events)
        if events:
            source_used.append("sqlite_summary")

    events = _sort_events(_dedupe(events))

    high = [e for e in events if _event_high_impact(e)]
    medium = [
        e for e in events
        if _importance_int(e.get("importance")) >= MEDIUM_IMPORTANCE_THRESHOLD
        and e not in high
    ]

    payload = {
        "date": iso_date,
        "lookback_days": lookback_days,
        "source": "+".join(source_used) if source_used else "none",
        "events": events,
        "high_importance_events": high,
        "medium_importance_events": medium,
        "has_high_impact_event": bool(high),
        "is_data_release_day": bool(events),
        "event_count": len(events),
        "high_impact_count": len(high),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        _write_cache(iso_date, payload)
    except Exception as exc:
        print(f"[econ_calendar] cache write failed: {exc}")

    return payload


def render_for_prompt(payload: dict[str, Any]) -> str:
    """Render the econ payload as a compact Markdown for the LLM prompt.

    精简策略：
    - 高影响事件：全部列出（已按地区过滤，只剩A股相关）
    - 中等重要事件：只保留A股核心地区，其他国家汇总计数
    - 低重要度事件：只给汇总计数
    """
    if not payload.get("events"):
        return "（今日及前一日均无重要经济数据 / 政策事件发布）"

    lines: list[str] = []

    # 高影响事件
    high = payload.get("high_importance_events", [])
    if high:
        lines.append(f"【高影响A股相关事件 ({len(high)} 条) — 优先权最高】")
        for e in high:
            lines.append(_render_event_line(e))

    # 中等重要事件：按地区分组，只保留A股核心地区
    medium = payload.get("medium_importance_events", [])
    a_share_medium = []
    other_medium_count = 0
    routine_hints = ("每日", "每日更新", "日更", "仓单", "持仓变动")
    for e in medium:
        region = e.get("region", "")
        name = e.get("event", "")
        # 排除日常更新类事件
        if any(h in name for h in routine_hints):
            other_medium_count += 1
            continue
        if any(r in region for r in A_SHARE_IMPACT_REGIONS):
            a_share_medium.append(e)
        else:
            other_medium_count += 1

    if a_share_medium:
        lines.append(f"\n【中等重要 - A股相关 ({len(a_share_medium)} 条)】")
        for e in a_share_medium:
            lines.append(_render_event_line(e))
    if other_medium_count:
        lines.append(f"\n（其他地区中等重要事件 {other_medium_count} 条已省略）")

    # 低重要度事件只给计数
    total_low = payload.get("event_count", 0) - len(high) - len(medium)
    if total_low > 0:
        lines.append(f"\n（低重要度事件 {total_low} 条已省略）")

    return "\n".join(lines)


def _render_event_line(event: dict[str, Any]) -> str:
    region = event.get("region") or ""
    name = event.get("event") or ""
    time_str = event.get("time") or ""
    nums = []
    if event.get("actual"):
        nums.append(f"公布={event['actual']}")
    if event.get("expected"):
        nums.append(f"预期={event['expected']}")
    if event.get("previous"):
        nums.append(f"前值={event['previous']}")
    imp = _importance_int(event.get("importance"))
    star = "★" * max(1, min(3, imp))
    nums_text = (" " + " / ".join(nums)) if nums else ""
    when = f" [{time_str}]" if time_str else ""
    return f"- {star} {region}{name}{when}{nums_text}"


__all__ = [
    "load_econ_payload",
    "render_for_prompt",
    "HIGH_IMPORTANCE_THRESHOLD",
    "HIGH_IMPACT_EVENT_KEYWORDS",
]
