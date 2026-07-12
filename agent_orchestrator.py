"""Orchestrate multi-turn chat: collection wizard + boundaries + existing Q&A."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from agent_kb import load_knowledge_base
from etf_agent_chat import (
    COMPETITION_HINTS,
    _format_competition,
    _format_competition_json,
    _run_today_prediction,
    _sync_knowledge_base,
    _wants_competition_json,
    _wants_run_prediction,
    handle_message as legacy_handle,
)
from info_collector import (
    DISCLAIMER,
    apply_field_answer,
    collection_prompt,
    missing_fields,
    next_field,
    ready_summary,
    try_extract_from_message,
    ui_block_for_field,
    wants_competition_mode,
    wants_personal_advice,
)
from personalized_advisor import (
    advice_ui_blocks,
    competition_ui_blocks,
    format_advice_markdown,
)
from live_personal_runner import run_live_personal_advice
import session_store as store
from strategy import OFFENSIVE_POOL, TRADING_POOL
from trading_calendar import is_trading_day


POOL_CODES = {str(x["code"]).zfill(6) for x in TRADING_POOL + OFFENSIVE_POOL}

# Common A-share stock names / patterns outside our ETF scope
STOCK_HINTS = (
    "个股", "股票代码", "买股票", "炒股", "选股",
    "茅台", "宁德时代", "比亚迪", "招商银行", "中国平安",
    "五粮液", "美的", "格力", "腾讯", "阿里巴巴", "中芯国际",
    "贵州茅台", "工商银行", "建设银行", "农业银行", "中国石油",
    "中国移动", "海康威视", "隆基", "阳光电源", "片仔癀",
)

INTERNAL_HINTS = (
    "评分公式", "闸门", "阈值", "源码", "源代码", "怎么算分",
    "内部逻辑", "程序文件", "daily_job", "position.py", "strategy.py",
    "MAX_SINGLE", "FORCE_POSITION", "实现细节",
)

OFF_TOPIC_HINTS = (
    "天气", "笑话", "写诗", "做饭", "游戏", "电影", "星座",
    "恋爱", "八卦", "足球比分", "彩票",
)


def _stock_out_of_scope(message: str) -> bool:
    msg = message or ""
    # explicit stock request
    if any(h in msg for h in STOCK_HINTS):
        return True
    # 6-digit code not in ETF pool
    for code in re.findall(r"\b(\d{6})\b", msg):
        if code not in POOL_CODES:
            # ask about buying/holding that code
            if any(w in msg for w in ("买", "卖", "持有", "推荐", "怎么样", "能买", "建议")):
                return True
    # "XX股票" pattern
    if re.search(r"[\u4e00-\u9fff]{2,8}股票", msg):
        # allow "ETF股票" style? rare — still redirect if not mentioning ETF
        if "ETF" not in msg.upper() and "基金" not in msg:
            return True
    return False


def _is_internal(message: str) -> bool:
    return any(h in (message or "") for h in INTERNAL_HINTS)


def _is_off_topic(message: str) -> bool:
    msg = message or ""
    if any(h in msg for h in OFF_TOPIC_HINTS):
        # but allow if also clearly about ETF
        if any(w in msg for w in ("ETF", "etf", "持仓", "投资", "比赛", "新闻")):
            return False
        return True
    return False


def _pack(
    sess: dict[str, Any],
    *,
    reply: str,
    intent: str,
    ui_blocks: list[dict[str, Any]] | None = None,
    via: str = "rule",
    kb_date: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    store.update_session(
        sess["session_id"],
        append_message={"role": "assistant", "intent": intent, "text": reply[:500]},
    )
    out: dict[str, Any] = {
        "reply": reply,
        "intent": intent,
        "via": via,
        "kb_date": kb_date,
        "ui_blocks": ui_blocks or [],
        "session": store.public_view(store.get_session(sess["session_id"]) or sess),
        "disclaimer": DISCLAIMER,
    }
    if extra:
        out.update(extra)
    return out


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _is_market_closed_today() -> bool:
    return not is_trading_day(_today_str())


def _market_closed_response(sess: dict[str, Any], message: str = "") -> dict[str, Any]:
    if message:
        store.update_session(
            sess["session_id"],
            append_message={"role": "user", "text": message},
        )
    store.update_session(
        sess["session_id"],
        state=store.STATE_IDLE,
        collect_step=None,
    )
    return _pack(
        sess,
        reply=(
            "今天 A 股市场休市，**暂无当日 ETF 投资建议**，也不会生成比赛提交结果。\n\n"
            "您仍可以查看最近交易日复盘、询问 ETF 新闻影响，或在下一个交易日再获取建议。"
        ),
        intent="market_closed",
    )


def _start_collection(sess: dict[str, Any], message: str) -> dict[str, Any]:
    profile = dict(sess.get("profile") or {})
    profile = try_extract_from_message(message, profile)
    field = next_field(profile)
    store.update_session(
        sess["session_id"],
        state=store.STATE_COLLECTING if field else store.STATE_READY,
        profile=profile,
        advice_mode="personal",
        collect_step=field,
        append_message={"role": "user", "text": message},
    )
    sess = store.get_session(sess["session_id"]) or sess

    if not field:
        return _generate_personal_advice(sess)

    reply = (
        "好的，我来帮您做今日 ETF 配置建议。\n\n"
        "本产品**只覆盖 ETF**，不提供个股建议。\n\n"
        + collection_prompt(field)
    )
    return _pack(
        sess,
        reply=reply,
        intent="collect_info",
        ui_blocks=[ui_block_for_field(field)],
        kb_date=None,
    )


def _continue_collection(
    sess: dict[str, Any],
    message: str,
    field_answer: dict[str, Any] | None,
) -> dict[str, Any]:
    profile = dict(sess.get("profile") or {})
    store.update_session(
        sess["session_id"],
        append_message={"role": "user", "text": message or str(field_answer)},
    )

    # Structured answer from UI
    if field_answer and field_answer.get("field"):
        field = str(field_answer["field"])
        value = field_answer.get("value")
        profile, err = apply_field_answer(profile, field, value)
        if err:
            store.update_session(sess["session_id"], profile=profile, collect_step=field)
            return _pack(
                sess,
                reply=err,
                intent="collect_info",
                ui_blocks=[ui_block_for_field(field)],
            )
    else:
        # Free text while collecting
        step = sess.get("collect_step") or next_field(profile)
        if step:
            profile, err = apply_field_answer(profile, step, message)
            if err:
                # try soft extract
                profile = try_extract_from_message(message, profile)
                if step in missing_fields(profile):
                    store.update_session(sess["session_id"], profile=profile, collect_step=step)
                    return _pack(
                        sess,
                        reply=err + "\n\n" + collection_prompt(step),
                        intent="collect_info",
                        ui_blocks=[ui_block_for_field(step)],
                    )
        else:
            profile = try_extract_from_message(message, profile)

    field = next_field(profile)
    store.update_session(
        sess["session_id"],
        profile=profile,
        collect_step=field,
        state=store.STATE_COLLECTING if field else store.STATE_READY,
    )
    sess = store.get_session(sess["session_id"]) or sess

    if field:
        return _pack(
            sess,
            reply=collection_prompt(field),
            intent="collect_info",
            ui_blocks=[ui_block_for_field(field)],
        )
    return _generate_personal_advice(sess)


def _generate_personal_advice(sess: dict[str, Any]) -> dict[str, Any]:
    if _is_market_closed_today():
        return _market_closed_response(sess)
    profile = sess.get("profile") or {}
    capital = float(profile.get("capital") or 500000)
    risk = str(profile.get("risk_preference") or "balanced")
    focus = str(profile.get("focus") or "auto")
    prefer_codes = list(profile.get("prefer_codes") or [])
    avoid_codes = list(profile.get("avoid_codes") or [])
    date_str = datetime.now().strftime("%Y-%m-%d")

    # Live run from base data (K-line / news / econ). Never overwrites competition.
    advice = run_live_personal_advice(
        capital=capital,
        risk_preference=risk,
        focus=focus,
        prefer_codes=prefer_codes,
        avoid_codes=avoid_codes,
        date_str=date_str,
        allow_news_fetch=True,
        use_llm=True,
        save_sandbox=True,
    )

    if not advice.get("ok"):
        return _pack(
            sess,
            reply=advice.get("error") or "现算建议失败，请稍后再试。",
            intent="run_error",
        )

    store.update_session(
        sess["session_id"],
        state=store.STATE_DONE,
        last_advice=advice,
        collect_step=None,
    )
    sess = store.get_session(sess["session_id"]) or sess

    prefix = ready_summary(profile) + "\n\n"
    reply = prefix + format_advice_markdown(advice)
    return _pack(
        sess,
        reply=reply,
        intent="personal_advice",
        ui_blocks=advice_ui_blocks(advice),
        kb_date=advice.get("date"),
        extra={"advice": advice, "live": True},
    )


def _handle_competition(sess: dict[str, Any], message: str) -> dict[str, Any]:
    if _is_market_closed_today():
        return _market_closed_response(sess, message)
    store.update_session(
        sess["session_id"],
        advice_mode="competition",
        append_message={"role": "user", "text": message},
    )
    today = datetime.now().strftime("%Y-%m-%d")
    kb = load_knowledge_base(today)
    used_fallback = False

    if _wants_run_prediction(message):
        # Chat may only create a first-run or read cache — never force-overwrite
        # competition artifacts from the public conversational UI.
        run = _run_today_prediction(skip_price_update=False, force=False)
        kb = run.get("kb") or load_knowledge_base(today)
        if kb is None:
            _sync_knowledge_base(today)
            kb = load_knowledge_base(today)
        if kb is None:
            return _pack(
                sess,
                reply="比赛预测未能生成：" + str((run or {}).get("error") or "暂无数据"),
                intent="run_error",
            )
        if run.get("protected") or run.get("skipped"):
            # keep going with cached holdings
            pass
    elif kb is None:
        kb = load_knowledge_base(None)
        used_fallback = kb is not None

    if kb is None:
        return _pack(
            sess,
            reply="暂无可用预测。请说「测一下今天」生成后再查看比赛格式。",
            intent="no_kb",
        )

    date_label = str(kb.get("date") or today)
    holdings = list(kb.get("competition_output") or [])
    note = ""
    if used_fallback and date_label != today:
        note = f"\n\n说明：今日尚未生成预测，以下为最近交易日 **{date_label}** 结果。需要最新请说「测一下今天」。\n"

    if _wants_competition_json(message) or "格式" in message or "json" in message.lower():
        reply = (
            f"**{date_label} 比赛提交 JSON**（资金固定 50 万）"
            + note
            + "\n"
            + _format_competition_json(kb)
            + "\n\n"
            + DISCLAIMER
        )
    else:
        reply = (
            _format_competition(kb)
            + note
            + "\n\n如需提交格式，请说「比赛提交格式」。\n\n"
            + DISCLAIMER
        )

    store.update_session(sess["session_id"], state=store.STATE_DONE, last_advice={
        "mode": "competition",
        "date": date_label,
        "holdings": holdings,
    })
    return _pack(
        sess,
        reply=reply,
        intent="competition",
        ui_blocks=competition_ui_blocks(holdings, date_label),
        kb_date=date_label,
    )


def handle_chat(
    message: str,
    *,
    session_id: str | None = None,
    date_str: str | None = None,
    field_answer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Main entry for the conversational frontend.
    Returns reply + ui_blocks + session state.
    """
    message = (message or "").strip()
    sess = store.ensure_session(session_id)

    # Empty message but field_answer present → continue collection
    if not message and not field_answer:
        return _pack(
            sess,
            reply="请输入您的问题，例如「今日投资建议」或「今日比赛预测」。",
            intent="empty",
        )

    # --- Boundary: stocks ---
    if message and _stock_out_of_scope(message):
        store.update_session(sess["session_id"], append_message={"role": "user", "text": message})
        return _pack(
            sess,
            reply=(
                "本产品**不将个股及其他非 ETF 标的**纳入投资建议范围，"
                "只提供 A 股 ETF 配置与解读。\n\n"
                "您可以问我：「今日投资建议」「为什么这么配」「某条新闻对 ETF 有何影响」"
                "或「今日比赛预测」。"
            ),
            intent="boundary_stock",
        )

    # --- Boundary: internals ---
    if message and _is_internal(message):
        store.update_session(sess["session_id"], append_message={"role": "user", "text": message})
        return _pack(
            sess,
            reply="这部分属于策略内部实现，不在对外问答范围。我可以解释今日持仓的新闻与宏观理由。",
            intent="boundary_internals",
        )

    # --- Boundary: off-topic ---
    if message and _is_off_topic(message) and sess.get("state") != store.STATE_COLLECTING:
        store.update_session(sess["session_id"], append_message={"role": "user", "text": message})
        return _pack(
            sess,
            reply="我是 ETF 投资助手，请把话题回到 ETF 配置、新闻影响或比赛预测上。",
            intent="off_topic",
        )

    requests_today_advice = bool(
        field_answer
        or sess.get("state") == store.STATE_COLLECTING
        or (message and wants_personal_advice(message))
        or (message and wants_competition_mode(message))
        or (message and _wants_run_prediction(message))
    )
    if requests_today_advice and _is_market_closed_today():
        return _market_closed_response(sess, message)

    # --- Continue collection if mid-wizard ---
    if sess.get("state") == store.STATE_COLLECTING or field_answer:
        # Allow escape to competition mid-collection
        if message and wants_competition_mode(message):
            return _handle_competition(sess, message)
        return _continue_collection(sess, message, field_answer)

    # --- Start personal advice wizard ---
    if message and wants_personal_advice(message):
        return _start_collection(sess, message)

    # --- Quick adjust after advice already given ---
    if (
        message
        and sess.get("state") == store.STATE_DONE
        and (sess.get("profile") or {}).get("capital")
        and any(
            w in message
            for w in (
                "改成", "换成", "改为", "偏好", "不要", "避开", "稳健", "进取", "均衡",
                "红利", "宽基", "成长", "行业", "防守", "激进", "重新配",
            )
        )
    ):
        from info_collector import parse_focus, parse_risk, try_extract_from_message

        profile = try_extract_from_message(message, dict(sess.get("profile") or {}))
        risk = parse_risk(message)
        if risk:
            profile["risk_preference"] = risk
        focus = parse_focus(message)
        if focus and (
            focus != "auto"
            or any(w in message for w in ("跟随", "按策略"))
        ):
            profile["focus"] = focus
        store.update_session(
            sess["session_id"],
            profile=profile,
            state=store.STATE_READY,
            append_message={"role": "user", "text": message},
        )
        sess = store.get_session(sess["session_id"]) or sess
        return _generate_personal_advice(sess)

    # --- Competition mode ---
    if message and wants_competition_mode(message):
        return _handle_competition(sess, message)

    # --- Explicit competition hints that legacy also catches ---
    if message and any(h in message for h in COMPETITION_HINTS) and not _wants_run_prediction(message):
        # "今日建议" without "投资" — treat as competition for backward compat
        # but "今日投资建议" already caught by personal
        if "投资" not in message and "个人" not in message and "配置" not in message:
            return _handle_competition(sess, message)

    # --- Delegate to legacy Q&A (why / news / pnl / run / llm) ---
    store.update_session(sess["session_id"], append_message={"role": "user", "text": message})
    result = legacy_handle(message, date_str=date_str)

    # Enrich with session + optional ui blocks from last advice
    ui_blocks: list[dict[str, Any]] = []
    intent = result.get("intent") or "etf_general"
    if intent in ("competition", "run_today_job", "run_today_cached"):
        today = datetime.now().strftime("%Y-%m-%d")
        kb = load_knowledge_base(today) or load_knowledge_base(result.get("kb_date"))
        if kb:
            ui_blocks = competition_ui_blocks(
                list(kb.get("competition_output") or []),
                kb.get("date") or today,
            )

    # If user asks why after personal advice, keep session
    reply = result.get("reply") or ""
    if intent == "why_pick" and sess.get("last_advice"):
        la = sess["last_advice"]
        if la.get("mode") == "personal" and la.get("holdings"):
            # already good from legacy
            pass

    return _pack(
        sess,
        reply=reply,
        intent=intent,
        ui_blocks=ui_blocks,
        via=result.get("via") or "rule",
        kb_date=result.get("kb_date"),
        extra={
            k: result[k]
            for k in ("kb_saved", "kb_updated_at")
            if k in result
        },
    )


def start_session() -> dict[str, Any]:
    sess = store.create_session()
    market_closed = _is_market_closed_today()
    if market_closed:
        welcome = (
            "你好，我是 **ETF 投资智能体**。\n\n"
            "今天 A 股市场休市，**暂无当日 ETF 投资建议**，也不会生成比赛提交结果。\n\n"
            "您仍可以查看最近交易日复盘，或询问新闻对 ETF 市场的潜在影响。\n\n"
            "本产品**只负责 ETF**，不提供个股建议。\n\n"
            + DISCLAIMER
        )
        options = [
            {"label": "最近交易日收益复盘", "value": "昨天赚了多少钱"},
            {"label": "今日筛选新闻", "value": "今日筛选后的新闻有哪些"},
            {"label": "新闻对 ETF 的影响", "value": "某条新闻对ETF有什么影响"},
        ]
    else:
        welcome = (
            "你好，我是 **ETF 投资智能体**。\n\n"
            "我可以帮你：\n"
            "- **今日投资建议**（先问资金与风格，再生成配置）\n"
            "- **今日比赛预测**（固定 50 万，输出提交 JSON）\n"
            "- 解释为什么这么配、解读新闻对 ETF 的影响\n\n"
            "本产品**只负责 ETF**，不提供个股建议。\n\n"
            + DISCLAIMER
        )
        options = [
            {"label": "今日投资建议", "value": "今日投资建议"},
            {"label": "今日比赛预测", "value": "今日比赛预测"},
            {"label": "为什么选这些 ETF", "value": "为什么选这些ETF"},
            {"label": "今日筛选新闻", "value": "今日筛选后的新闻有哪些"},
        ]
    return {
        "session": store.public_view(sess),
        "reply": welcome,
        "intent": "greeting",
        "ui_blocks": [
            {
                "type": "choices",
                "field": "entry",
                "question": "今日休市，您可以查看：" if market_closed else "您想先做什么？",
                "options": options,
            }
        ],
        "disclaimer": DISCLAIMER,
        "market_closed": market_closed,
    }
