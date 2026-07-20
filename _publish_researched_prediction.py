"""Publish a human-researched ETF competition prediction.

The research may be performed outside the automated strategy, but execution
still obeys the competition contract: ETF allowlist, CNY 500,000 capital,
previous-session close, 100-share lots, one dated official output, and the
08:30 Asia/Shanghai deadline guard.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from agent_kb import rebuild_knowledge_base
from competition_guard import COMPETITION_CAPITAL
from daily_job import save_outputs, validate_execution_consistency
from pool import ALL_POOL
from trading_calendar import is_trading_day, previous_trading_day


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = DATA_DIR / "daily_output"
SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DEADLINE = time(8, 30)


def _etf_by_code(symbol: str) -> dict[str, Any]:
    code = str(symbol).zfill(6)
    for item in ALL_POOL:
        if str(item["code"]).zfill(6) == code:
            return dict(item)
    raise ValueError(f"标的不在比赛 ETF 白名单: {code}")


def _previous_close(symbol: str, date_str: str, data_dir: Path) -> tuple[str, float]:
    prior = previous_trading_day(date_str).isoformat()
    path = data_dir / f"{symbol}.csv"
    if not path.exists():
        raise FileNotFoundError(f"缺少行情文件: {path}")
    frame = pd.read_csv(path).rename(columns={
        "日期": "date",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
    })
    if "date" not in frame.columns or "close" not in frame.columns:
        raise ValueError(f"行情列不完整: {path}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    row = frame.loc[frame["date"] == prior]
    if row.empty:
        raise ValueError(f"{symbol} 缺少上一交易日 {prior} 收盘价")
    close = float(pd.to_numeric(row.iloc[-1]["close"], errors="coerce"))
    if not close > 0:
        raise ValueError(f"{symbol} 上一交易日收盘价无效: {close}")
    return prior, close


def build_prediction(
    *,
    date_str: str,
    symbol: str,
    allocation: float,
    reason: str,
    sources: list[dict[str, str]],
    data_dir: Path = DATA_DIR,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if not is_trading_day(date_str):
        raise ValueError(f"{date_str} 不是 A 股交易日")
    if not 0 < allocation <= 1:
        raise ValueError("allocation 必须在 (0, 1] 范围内")
    if not reason.strip():
        raise ValueError("必须填写公开信息研究理由")
    if not sources or any(not x.get("title") or not x.get("url") for x in sources):
        raise ValueError("至少提供一条带标题和 URL 的可靠来源")

    item = _etf_by_code(symbol)
    code = str(item["code"]).zfill(6)
    prior_date, close = _previous_close(code, date_str, data_dir)
    budget = float(COMPETITION_CAPITAL) * allocation
    volume = int(budget // close // 100 * 100)
    if volume <= 0:
        raise ValueError("按上一交易日收盘价计算后不足一手")
    amount = round(volume * close, 2)
    actual_ratio = amount / float(COMPETITION_CAPITAL)

    accepted_articles = [
        {
            "title": source["title"].strip(),
            "url": source["url"].strip(),
            "quality": "strong",
            "reason": "人工研究已核验来源与发布日期",
            "theme_scores": {code: 0.65},
        }
        for source in sources
    ]
    news_signal = {
        "source": "human_public_research",
        "article_count": len(accepted_articles),
        "accepted_count": len(accepted_articles),
        "strong_count": len(accepted_articles),
        "confidence": 0.7,
        "market_sentiment": "selective",
        "accepted_articles": accepted_articles,
    }
    held = {
        "code": code,
        "name": item["name"],
        "volume": volume,
        "latest_price": close,
        "amount": amount,
        "weight": round(actual_ratio, 6),
        "reason": reason.strip(),
    }
    result = {
        "date": date_str,
        "mode": "human_public_research",
        "market_reason": reason.strip(),
        "manual_research": {
            "source": "human_public_research",
            "price_basis": "previous_trading_day_close",
            "price_date": prior_date,
            "reference_close": close,
            "requested_allocation": allocation,
            "sources": sources,
        },
        "llm_trace": {
            "summary_zh": reason.strip(),
            "hard_rules_applied": [
                "manual_public_research",
                "competition_etf_allowlist",
                "previous_close_execution",
                "round_lot_100",
            ],
        },
        "summary": {
            "capital": float(COMPETITION_CAPITAL),
            "used": amount,
            "cash": round(float(COMPETITION_CAPITAL) - amount, 2),
            "invest_ratio": round(actual_ratio, 6),
            "held_stocks": [held],
        },
    }
    submit = [{"symbol": code, "symbol_name": item["name"], "volume": volume}]
    validate_execution_consistency(result, submit)
    return result, submit, news_signal


def _parse_source(raw: str) -> dict[str, str]:
    title, sep, url = raw.partition("|")
    if not sep or not title.strip() or not url.strip():
        raise argparse.ArgumentTypeError("source 格式必须为 标题|URL")
    return {"title": title.strip(), "url": url.strip()}


def _deadline_allows(date_str: str, *, replace: bool) -> None:
    if not replace:
        return
    now = datetime.now(SHANGHAI)
    if date_str == now.date().isoformat() and now.time() > DEFAULT_DEADLINE:
        raise ValueError("08:30 后禁止替换当日正式比赛预测")


def main() -> int:
    parser = argparse.ArgumentParser(description="发布人工公开信息研究 ETF 比赛预测")
    parser.add_argument("--date", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--allocation", required=True, type=float)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source", action="append", type=_parse_source, required=True)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()

    submit_path = OUTPUT_DIR / f"{args.date}_submit.json"
    full_path = OUTPUT_DIR / f"{args.date}_full.json"
    if (submit_path.exists() or full_path.exists()) and not args.replace:
        raise SystemExit(f"预测已存在，拒绝覆盖: {args.date}；确认后使用 --replace")
    _deadline_allows(args.date, replace=args.replace)

    result, submit, news_signal = build_prediction(
        date_str=args.date,
        symbol=args.symbol,
        allocation=args.allocation,
        reason=args.reason,
        sources=args.source,
    )
    submit_path, full_path = save_outputs(
        args.date,
        submit,
        result,
        news_signal,
        pnl_report=None,
        capital=float(COMPETITION_CAPITAL),
    )
    kb_path = rebuild_knowledge_base(args.date)
    print(json.dumps({
        "submit": str(submit_path),
        "full": str(full_path),
        "knowledge_base": str(kb_path),
        "competition_output": submit,
        "manual_research": result["manual_research"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
