"""Single daily workflow: news -> strict filter -> strategy -> competition output."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from daily_pnl import review_previous_prediction, write_pnl_report
from econ_calendar import load_econ_payload
import llm_client
import llm_decider
from news_fetcher import fetch_news_articles
from news_signal import build_news_signal, summarize_for_llm
from news_llm_scorer import score_news_with_llm, merge_llm_into_news_signal
from strategy import (
    OFFENSIVE_POOL,
    OFFENSIVE_ON_THRESHOLD,
    TRADING_POOL,
    _calc_short_race_features,
    _get_price_for_decision,
    market_avg_score,
    run_decision,
)
from theme_signal import save_theme_signal
from update_local_csv import update_local_etfs


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
ARCHIVE_DIR = OUTPUT_DIR / "archive"


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)

# ═══════════════════════════════════════════════════
# 严格错误处理：关键数据源质量不达标时终止预测
# ═══════════════════════════════════════════════════
MIN_NEWS_ARTICLES = 5          # 有效新闻至少 5 条，否则视为数据源故障
MIN_NEWS_FOR_STRONG = 1         # 至少 1 条强信号新闻
MIN_ECON_EVENTS = 1            # 经济日历至少 1 条事件（否则可能是网络故障）
DATA_QUALITY_WARN_FLAGS: list[str] = []   # 低置信度标记，写入输出文件

def _check_critical_data_quality(
    news_signal: dict[str, Any],
    econ_payload: dict[str, Any],
) -> bool:
    """检查关键数据源质量，不达标时返回 False（应终止预测）。

    分级策略：
    - 致命：行情更新失败（由 update_local_etfs 内部处理）
    - 致命：有效新闻为 0 且无任何新闻抓到
    - 致命：经济日历完全为空且 akshare_live 也未尝试
    - 警告：有效新闻不足但 > 0 → 标记低置信度，继续
    - 警告：经济日历为空但新闻足够 → 降低仓位上限，继续
    """
    global DATA_QUALITY_WARN_FLAGS
    DATA_QUALITY_WARN_FLAGS = []
    fatal = False

    # --- 新闻质量检查 ---
    article_count = news_signal.get("article_count", 0)
    accepted_count = news_signal.get("accepted_count", 0)
    strong_count = news_signal.get("strong_count", 0)
    max_abs = news_signal.get("max_abs_theme", 0.0)

    if article_count == 0:
        log("[CRITICAL] 未抓取到任何新闻（所有数据源失败），终止预测。")
        return False

    if accepted_count == 0:
        log(f"[CRITICAL] 抓到 {article_count} 条新闻但全部被筛选拒绝，"
            f"无法生成有效信号，终止预测。")
        return False

    if accepted_count < MIN_NEWS_ARTICLES:
        DATA_QUALITY_WARN_FLAGS.append(f"有效新闻仅{accepted_count}条(阈值{MIN_NEWS_ARTICLES})")
        log(f"[WARN] 有效新闻仅 {accepted_count} 条（阈值 {MIN_NEWS_ARTICLES}），"
            f"标记为低置信度但继续运行。")

    if strong_count == 0 and max_abs < 0.2:
        DATA_QUALITY_WARN_FLAGS.append("无强信号新闻且主题分极低")
        log("[WARN] 无强信号新闻且主题信号极弱，预测可靠性下降。")

    # --- 经济日历质量检查 ---
    econ_source = econ_payload.get("source", "none")
    econ_events = econ_payload.get("event_count", 0)

    if econ_events == 0 and econ_source == "none":
        # akshare_live 和 sqlite_summary 都没有数据
        log("[CRITICAL] 经济日历完全为空（所有数据源失败），终止预测。")
        return False

    if econ_events == 0:
        DATA_QUALITY_WARN_FLAGS.append("经济日历为空，仓位上限降至50%")
        log("[WARN] 经济日历为空（可能是单日确实无事件），仓位上限降至 50%。")

    return True


def _save_error_report(date_str: str, reason: str, partial_data: dict | None = None) -> Path:
    """数据源故障时保存错误报告（不生成 submit.json），便于排查。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    report_path = OUTPUT_DIR / f"{date_str}_error.json"
    report = {
        "date": date_str,
        "error": True,
        "reason": reason,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "partial_data": partial_data or {},
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def _consecutive_up_days(df: pd.DataFrame) -> int:
    close = df["close"].dropna()
    if len(close) < 2:
        return 0
    count = 0
    for i in range(len(close) - 1, 0, -1):
        if float(close.iloc[i]) > float(close.iloc[i - 1]):
            count += 1
        else:
            break
    return count


def build_trend_context(date_str: str) -> dict[str, dict[str, Any]]:
    context: dict[str, dict[str, Any]] = {}
    for item in TRADING_POOL:
        code = item["code"]
        df = _get_price_for_decision(code, date_str)
        features = _calc_short_race_features(df)
        if not features or df is None:
            continue
        context[code] = {
            **features,
            "consecutive_up_days": _consecutive_up_days(df),
        }
    return context


def _split_articles_by_close(articles: list[dict], date_str: str) -> tuple[list[dict], list[dict]]:
    """按上一交易日 15:00 切分：盘后新鲜 vs 更早陈旧（周一用周五 15:00）。"""
    from news_time_split import post_close_cutoff, split_articles_by_post_close

    fresh, stale, cutoff = split_articles_by_post_close(articles, date_str)
    prev = cutoff.date()
    log(
        f"新闻时间切割(>{prev} 15:00 为新鲜): "
        f"新鲜 {len(fresh)} 条, 陈旧 {len(stale)} 条"
    )
    return fresh, stale


def _process_news_pool(articles: list[dict], trend_context: dict, pool_codes: list[str],
                       date_str: str, label: str) -> dict[str, Any]:
    """对一批新闻跑完两级筛选流程，返回独立的信号字典。"""
    if not articles:
        return {"theme_scores": {}, "accepted_articles": [], "accepted_count": 0,
                "strong_count": 0, "weak_count": 0, "source": "none"}

    signal = build_news_signal(articles, trend_context=trend_context, date=date_str)
    signal["_original_theme_scores"] = dict(signal.get("theme_scores", {}))

    accepted = signal.get("accepted_articles", [])
    skip_news_llm = os.environ.get("ETF_AGENT_SKIP_NEWS_LLM", "0").strip() == "1"
    if not skip_news_llm and accepted and pool_codes:
        try:
            llm_results = score_news_with_llm(accepted, pool_codes)
            if llm_results:
                signal = merge_llm_into_news_signal(signal, llm_results)
        except Exception as exc:
            log(f"[{label}] LLM语义评分异常: {exc}，保留关键词评分。")

    return signal


def build_daily_news_signal(date_str: str, cutoff_time: str) -> dict[str, Any]:
    log(f"抓取 {date_str} {cutoff_time} 前可用新闻...")
    articles = fetch_news_articles(date_str, cutoff_time=cutoff_time)
    log(f"抓到新闻 {len(articles)} 条，开始按时间分层处理...")
    trend_context = build_trend_context(date_str)
    pool_codes = [str(item["code"]).zfill(6) for item in TRADING_POOL]

    # ── 时间切割：昨日收盘后 vs 昨日收盘前 ──
    fresh_articles, stale_articles = _split_articles_by_close(articles, date_str)

    # ── 两级筛选分别处理 ──
    fresh_signal = _process_news_pool(fresh_articles, trend_context, pool_codes, date_str, "FRESH")
    stale_signal = _process_news_pool(stale_articles, trend_context, pool_codes, date_str, "STALE")

    # ── 合并为统一的信号结构，新增 fresh/stale 分层字段 ──
    fresh_scores = fresh_signal.get("theme_scores", {})
    stale_scores = stale_signal.get("theme_scores", {})
    fresh_acc = fresh_signal.get("accepted_count", 0)
    stale_acc = stale_signal.get("accepted_count", 0)
    fresh_str = fresh_signal.get("strong_count", 0)
    stale_str = stale_signal.get("strong_count", 0)

    signal = {
        "date": date_str,
        "source": "split_fresh_stale",
        "cutoff_time": cutoff_time,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 新鲜新闻（盘后至今 — 高权重）
        "fresh_theme_scores": fresh_scores,
        "fresh_accepted_count": fresh_acc,
        "fresh_strong_count": fresh_str,
        "fresh_accepted_articles": fresh_signal.get("accepted_articles", []),
        # 陈旧新闻（盘中及更早 — 低权重参考）
        "stale_theme_scores": stale_scores,
        "stale_accepted_count": stale_acc,
        "stale_strong_count": stale_str,
        "stale_accepted_articles": stale_signal.get("accepted_articles", []),
        # 向后兼容旧字段
        "theme_scores": fresh_scores,
        "scores": fresh_scores,
        "accepted_count": fresh_acc + stale_acc,
        "strong_count": fresh_str + stale_str,
        "weak_count": (fresh_acc - fresh_str) + (stale_acc - stale_str),
        "article_count": len(articles),
        "rejected_count": len(articles) - fresh_acc - stale_acc,
        "accepted_articles": fresh_signal.get("accepted_articles", []) + stale_signal.get("accepted_articles", []),
        "raw_articles": articles[:80],
        "confidence": fresh_signal.get("confidence", 0.0),
        "market_sentiment": fresh_signal.get("market_sentiment", 0.0),
        "max_abs_theme": fresh_signal.get("max_abs_theme", 0.0),
        "hot_keywords": fresh_signal.get("hot_keywords", []),
        "auto_news": {
            "enabled": True,
            "article_count": len(articles),
            "confidence": fresh_signal.get("confidence", 0.0),
            "market_sentiment": fresh_signal.get("market_sentiment", 0.0),
            "catalyst_hits": fresh_signal.get("catalyst_hits", 0),
            "max_abs_theme": fresh_signal.get("max_abs_theme", 0.0),
        },
    }

    path = save_theme_signal(signal, date_str)
    log(f"新闻分层完成: 新鲜={fresh_acc}条(强{fresh_str}) 陈旧={stale_acc}条(强{stale_str})")
    log(f"新闻信号保存: {path}")
    return signal


def build_llm_decision(
    date_str: str,
    capital: float,
    news_signal: dict[str, Any],
    econ_payload: dict[str, Any],
) -> dict[str, Any] | None:
    """调 LLM 决策；无 key/失败时返回 None，由 strategy 走纯规则降级。"""
    if not llm_client.is_available():
        log("[LLM] DEEPSEEK_API_KEY 未配置，跳过 LLM，自动降级到纯规则路径。")
        return None

    pool = [dict(item) for item in TRADING_POOL]
    avg_score = market_avg_score(date_str)
    if avg_score is not None and avg_score >= OFFENSIVE_ON_THRESHOLD:
        pool.extend([dict(item) for item in OFFENSIVE_POOL])

    pool_features: dict[str, dict[str, Any]] = {}
    for item in pool:
        code = str(item["code"]).zfill(6)
        df = _get_price_for_decision(code, date_str)
        feats = _calc_short_race_features(df)
        if feats:
            pool_features[code] = feats

    news_summary = summarize_for_llm(news_signal)
    log(f"[LLM] 调 DeepSeek 决策（新闻摘要 {len(news_summary)} 条；经济事件 "
        f"high={econ_payload.get('high_impact_count', 0)} total={econ_payload.get('event_count', 0)}）...")
    try:
        payload = llm_decider.decide(
            date_str=date_str,
            capital=capital,
            pool=pool,
            pool_features=pool_features,
            econ_payload=econ_payload,
            news_signal=news_signal,
            news_summary=news_summary,
        )
    except Exception as exc:
        log(f"[LLM] decide 抛错: {exc}; 走纯规则降级。")
        return None
    if payload is None:
        log("[LLM] 调用失败/不可用，走纯规则降级。")
        return None
    decision = payload.get("decision", {})
    log(
        "[LLM] OK: "
        f"regime={decision.get('regime')} cash={decision.get('cash_decision')} "
        f"ratio={decision.get('position_ratio_hint')} "
        f"per_etf={len(decision.get('per_etf_view') or [])} "
        f"cache_hit={payload.get('cache_hit')} usage={payload.get('usage')}"
    )
    return payload


def to_competition_output(result: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for item in result.get("summary", {}).get("held_stocks", []):
        amount = float(item.get("amount") or 0.0)
        price = float(item.get("latest_price") or 0.0)
        if amount <= 0 or price <= 0:
            continue
        # ETF 按 100 份一手取整，保证输出可交易。
        volume = int(amount // price // 100 * 100)
        if volume <= 0:
            continue
        out.append({
            "symbol": str(item["code"]).zfill(6),
            "symbol_name": item["name"],
            "volume": volume,
        })
    return out


def save_outputs(
    date_str: str,
    competition_output: list[dict[str, Any]],
    full_result: dict[str, Any],
    news_signal: dict[str, Any],
    pnl_report: dict[str, Any] | None,
    *,
    econ_payload: dict[str, Any] | None = None,
    llm_payload: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    submit_path = OUTPUT_DIR / f"{date_str}_submit.json"
    full_path = OUTPUT_DIR / f"{date_str}_full.json"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for path in (submit_path, full_path):
        if path.exists():
            backup = ARCHIVE_DIR / f"{path.stem}_{stamp}{path.suffix}"
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    submit_path.write_text(json.dumps(competition_output, ensure_ascii=False, indent=2), encoding="utf-8")
    full_payload = {
        "date": date_str,
        "competition_output": competition_output,
        "news_signal": news_signal,
        "econ_calendar": econ_payload,
        "llm_trace": full_result.get("llm_trace"),
        "llm_meta": {
            "model": (llm_payload or {}).get("model"),
            "usage": (llm_payload or {}).get("usage"),
            "cache_hit": (llm_payload or {}).get("cache_hit"),
            "prompt_hash": (llm_payload or {}).get("prompt_hash"),
            "cached_at": (llm_payload or {}).get("cached_at"),
            "prompt_file": llm_decider.PROMPT_PATH.name,
        } if llm_payload else None,
        "strategy_result": full_result,
        "previous_pnl": pnl_report,
    }
    full_path.write_text(json.dumps(full_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return submit_path, full_path


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF daily news-driven prediction job.")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    parser.add_argument("--capital", type=float, default=float(os.environ.get("CAPITAL", "500000")))
    parser.add_argument("--cutoff", default="09:30")
    parser.add_argument("--skip-price-update", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing prediction for --date (default: skip if already run).",
    )
    args = parser.parse_args()

    from daily_run_guard import has_daily_run, load_submit

    if not args.force and has_daily_run(args.date):
        cached = load_submit(args.date)
        log(f"{args.date} 今日预测已存在，跳过重复运行（仅加 --force 可覆盖）。")
        print("\n=== COMPETITION OUTPUT (已有，未重跑) ===")
        print(json.dumps(cached, ensure_ascii=False, indent=2))
        return 0

    target_date = pd.to_datetime(args.date).date()
    today = datetime.now().date()
    if target_date > today:
        raise SystemExit(f"不能生成未来日期预测: {args.date}")
    # 历史日期会覆盖已有预测且会用到当日 K 线，相当于回溯作弊；统一拒绝。
    if target_date < today:
        raise SystemExit(
            f"不能为历史日期重新生成预测: {args.date}。"
            "如需研究历史表现，请使用 run_news_backtest.py。"
        )

    os.environ.setdefault("ETF_AGENT_ALLOW_NETWORK", "1")
    os.environ.setdefault("ETF_AGENT_STRICT_DATA", "1")

    # 必须先更新行情再复盘，否则上一日 open/close 可能是旧数据（显示 0 元）。
    if not args.skip_price_update:
        log("更新 ETF 行情 CSV...")
        update_local_etfs(log_fn=lambda m: log(m))

    log("复盘上一日预测收益...")
    pnl_report = review_previous_prediction(args.date)
    pnl_path = write_pnl_report(pnl_report)
    log(f"复盘报告: {pnl_path}")

    # A 股周六周日休市：不生成预测，只保留上一日复盘。
    if target_date.weekday() >= 5:
        weekday_name = "周六" if target_date.weekday() == 5 else "周日"
        log(f"{args.date} 是{weekday_name}，A 股休市，今天没有策略建议。")
        if pnl_report is not None:
            print(f"\n上一日收益: {pnl_report['total_pnl']:+.2f} 元 ({pnl_report['prediction_date']})")
        print("\n=== 今日休市 ===\n今天 A 股休市，没有策略建议。")
        return 0

    news_signal = build_daily_news_signal(args.date, args.cutoff)

    log("加载经济日历（仓位风控与决策提示输入）...")
    econ_payload = load_econ_payload(args.date, allow_live=True, refresh=True)
    log(
        f"[Econ] events={econ_payload.get('event_count', 0)} "
        f"high={econ_payload.get('high_impact_count', 0)} "
        f"has_high_impact={econ_payload.get('has_high_impact_event')} "
        f"source={econ_payload.get('source')}"
    )

    # ═══ 严格数据质量检查 ═══
    if not _check_critical_data_quality(news_signal, econ_payload):
        error_path = _save_error_report(
            args.date,
            reason="数据源质量不达标，终止预测",
            partial_data={
                "news_signal_summary": {
                    "article_count": news_signal.get("article_count"),
                    "accepted_count": news_signal.get("accepted_count"),
                    "source": news_signal.get("source"),
                },
                "econ_summary": {
                    "event_count": econ_payload.get("event_count"),
                    "source": econ_payload.get("source"),
                },
            },
        )
        log(f"错误报告已保存: {error_path}")
        print(f"\n=== 预测终止 ===\n原因: 数据源质量不达标\n详情: {error_path}")
        return 1

    # 经济日历为空时降低仓位上限
    if econ_payload.get("event_count", 0) == 0:
        os.environ["FORCE_POSITION_CAP"] = "0.50"
        log("[QUALITY] 经济日历为空，已设置 FORCE_POSITION_CAP=0.50")

    llm_payload = build_llm_decision(args.date, args.capital, news_signal, econ_payload)
    llm_decision = (llm_payload or {}).get("decision")

    log("运行大模型+规则融合策略..." if llm_decision else "无大模型决策，运行纯规则策略...")
    result = run_decision(
        args.date,
        args.capital,
        llm_decision=llm_decision,
        econ_payload=econ_payload,
    )
    competition_output = to_competition_output(result)
    submit_path, full_path = save_outputs(
        args.date,
        competition_output,
        result,
        news_signal,
        pnl_report,
        econ_payload=econ_payload,
        llm_payload=llm_payload,
    )

    # 将数据质量警告写入 full.json
    if DATA_QUALITY_WARN_FLAGS:
        try:
            full_data = json.loads(full_path.read_text(encoding="utf-8"))
            full_data.setdefault("data_quality_warnings", []).extend(DATA_QUALITY_WARN_FLAGS)
            full_data["confidence"] = "low"
            full_path.write_text(
                json.dumps(full_data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    log(f"比赛格式输出: {submit_path}")
    log(f"完整记录输出: {full_path}")

    try:
        from agent_kb import rebuild_knowledge_base

        kb_path = rebuild_knowledge_base(args.date)
        log(f"智能体知识库已更新: {kb_path}")
    except Exception as exc:
        log(f"智能体知识库更新失败（不影响预测）: {exc}")

    print("\n=== COMPETITION OUTPUT ===")
    print(json.dumps(competition_output, ensure_ascii=False, indent=2))
    if llm_decision and llm_decision.get("summary_zh"):
        print(f"\nLLM 摘要: {llm_decision['summary_zh']}")
    if pnl_report is not None:
        print(f"\n上一日收益: {pnl_report['total_pnl']:+.2f} 元 ({pnl_report['prediction_date']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
