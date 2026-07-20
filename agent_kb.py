"""Daily knowledge base for the ETF investment agent (user-facing, no inner logic)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from strategy import OFFENSIVE_POOL, TRADING_POOL
from trading_calendar import next_trading_day


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
NEWS_DIR = BASE_DIR / "data" / "daily_news_signal"
KB_DIR = BASE_DIR / "data" / "agent_kb"

ETF_ALIASES: dict[str, str] = {}
for item in TRADING_POOL + OFFENSIVE_POOL:
    code = str(item["code"]).zfill(6)
    ETF_ALIASES[code] = code
    ETF_ALIASES[item["name"]] = code
    ETF_ALIASES[item["name"].replace("ETF", "")] = code


def _read_json(path: Path | None) -> dict[str, Any] | list[Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _direction_label(score: float) -> str:
    if score >= 0.35:
        return "偏多"
    if score <= -0.35:
        return "偏空"
    return "中性"


def _articles_for_code(articles: list[dict[str, Any]], code: str, limit: int = 5) -> list[dict[str, Any]]:
    linked = []
    for art in articles:
        themes = art.get("theme_scores") or {}
        if code not in themes:
            continue
        sc = float(themes[code])
        linked.append({
            "title": art.get("title", ""),
            "url": art.get("url", ""),
            "quality": art.get("quality", ""),
            "direction": _direction_label(sc),
            "strength": round(abs(sc), 3),
        })
    linked.sort(key=lambda x: x["strength"], reverse=True)
    return linked[:limit]


def rebuild_knowledge_base(date_str: str) -> Path:
    """Build user-facing KB from daily full output + news signal."""
    KB_DIR.mkdir(parents=True, exist_ok=True)
    full_path = OUTPUT_DIR / f"{date_str}_full.json"
    news_path = NEWS_DIR / f"{date_str}.json"

    full = _read_json(full_path) or {}
    if not full:
        raise FileNotFoundError(f"缺少当日完整记录: {full_path}")

    news = _read_json(news_path) or full.get("news_signal") or {}
    accepted = list(news.get("accepted_articles") or [])[:40]

    strategy = full.get("strategy_result") or {}
    llm_trace = strategy.get("llm_trace") or full.get("llm_trace") or {}
    summary = strategy.get("summary") or {}
    held = list(summary.get("held_stocks") or [])
    competition = list(full.get("competition_output") or [])

    per_etf = {str(x.get("code", "")).zfill(6): x for x in (llm_trace.get("per_etf_view") or [])}

    positions: list[dict[str, Any]] = []
    for pick in competition:
        code = str(pick.get("symbol", "")).zfill(6)
        vol = int(pick.get("volume") or 0)
        name = pick.get("symbol_name", "")
        reason = ""
        for h in held:
            if str(h.get("code", "")).zfill(6) == code:
                reason = str(h.get("reason") or "")
                name = name or h.get("name", "")
                break
        if not reason:
            pv = per_etf.get(code) or {}
            reason = str(pv.get("reason") or llm_trace.get("summary_zh") or "基于当日资讯与宏观环境综合判断。")
        positions.append({
            "symbol": code,
            "symbol_name": name,
            "volume": vol,
            "reason": reason.replace("LLM: ", "").strip(),
            "related_news": _articles_for_code(accepted, code, limit=4),
        })

    news_digest: list[dict[str, Any]] = []
    for art in accepted[:25]:
        themes = art.get("theme_scores") or {}
        links = []
        for code, sc in sorted(themes.items(), key=lambda kv: abs(float(kv[1])), reverse=True)[:4]:
            code = str(code).zfill(6)
            name = next((p["name"] for p in TRADING_POOL + OFFENSIVE_POOL if p["code"] == code), code)
            links.append({
                "symbol": code,
                "symbol_name": name,
                "direction": _direction_label(float(sc)),
                "strength": round(abs(float(sc)), 3),
            })
        news_digest.append({
            "title": art.get("title", ""),
            "url": art.get("url", ""),
            "quality": art.get("quality", ""),
            "screening_note": _quality_note(art.get("quality"), art.get("reason")),
            "linked_etfs": links,
        })

    econ_events = []
    econ = full.get("econ_calendar") or full.get("econ_payload") or {}
    for ev in (econ.get("events") or [])[:12]:
        if int(ev.get("importance") or 0) < 2:
            continue
        econ_events.append({
            "time": ev.get("time") or ev.get("pub_time") or "",
            "name": ev.get("name") or ev.get("event") or "",
            "importance": ev.get("importance"),
            "region": ev.get("region") or ev.get("country") or "",
        })

    kb: dict[str, Any] = {
        "date": date_str,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "competition_output": competition,
        "is_empty_position": len(competition) == 0,
        "decision_summary_zh": (llm_trace.get("summary_zh") or "").strip(),
        "market_context_zh": (strategy.get("market_reason") or "").split("；")[0][:200],
        "positions": positions,
        "news_stats": {
            "article_count": news.get("article_count", 0),
            "accepted_count": news.get("accepted_count", 0),
            "strong_count": news.get("strong_count", 0),
            "confidence": news.get("confidence"),
            "market_sentiment": news.get("market_sentiment"),
            "hot_keywords": (news.get("hot_keywords") or [])[:12],
        },
        "news_digest": news_digest,
        "econ_headlines": econ_events,
        "scope_note": (
            "本智能体仅解答 A 股 ETF 日内配置、赛事实盘建议、已筛选新闻与持仓解读；"
            "不披露内部评分公式、闸门阈值与程序实现细节。"
        ),
    }

    day_path = KB_DIR / f"{date_str}.json"
    day_path.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path = KB_DIR / "latest.json"
    should_update_latest = True
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest_date = str(latest.get("date") or "")
            # 仅允许更“新”的日期更新 latest，避免旧日期重建覆盖新日期。
            if latest_date and latest_date > date_str:
                should_update_latest = False
        except Exception:
            should_update_latest = True
    if should_update_latest:
        latest_path.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
    return day_path


def _quality_note(quality: str | None, reason: str | None) -> str:
    if quality == "strong":
        return "已通过筛选：含具体事件且能对应 ETF 板块"
    if quality == "weak":
        return "弱信号新闻，已降权处理"
    return str(reason or "已纳入筛选")


def load_knowledge_base(date_str: str | None = None) -> dict[str, Any] | None:
    KB_DIR.mkdir(parents=True, exist_ok=True)
    if date_str:
        path = KB_DIR / f"{date_str}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        # 指定日期时不回退 latest，避免串到其他日期的数据。
        return None
    latest = KB_DIR / "latest.json"
    if latest.exists():
        return json.loads(latest.read_text(encoding="utf-8"))
    # 尝试从最近 full 自动构建
    files = sorted(OUTPUT_DIR.glob("*_full.json"))
    if not files:
        return None
    d = files[-1].name.split("_")[0]
    try:
        rebuild_knowledge_base(d)
        return load_knowledge_base(d)
    except Exception:
        return None


def load_upcoming_researched_knowledge_base(
    as_of_date: str,
) -> dict[str, Any] | None:
    """Load a locked manual-research result for the next trading session only."""
    next_date = next_trading_day(as_of_date).isoformat()
    full = _read_json(OUTPUT_DIR / f"{next_date}_full.json")
    if not isinstance(full, dict):
        return None
    strategy = full.get("strategy_result") or {}
    if strategy.get("mode") != "human_public_research":
        return None
    return load_knowledge_base(next_date)


def resolve_etf_code(text: str) -> str | None:
    t = text.upper().replace(" ", "")
    for alias, code in sorted(ETF_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias.upper() in t or alias in text:
            return code
    if len(t) == 6 and t.isdigit():
        return t
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild ETF agent knowledge base.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    args = parser.parse_args()
    path = rebuild_knowledge_base(args.date)
    print(f"OK: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
