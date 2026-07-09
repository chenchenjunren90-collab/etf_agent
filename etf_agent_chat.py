"""ETF investment agent dialogue — DeepSeek-driven, grounded on daily KB."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_kb import ETF_ALIASES, load_knowledge_base, rebuild_knowledge_base, resolve_etf_code
from daily_pnl import _load_bar
import llm_client
import pandas as pd
from news_signal import score_news_article
from strategy import OFFENSIVE_POOL, TRADING_POOL


POOL_BY_CODE = {str(x["code"]).zfill(6): x for x in TRADING_POOL + OFFENSIVE_POOL}
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 直接走规则的强格式问题
COMPETITION_HINTS = (
    "今日比赛", "今天比赛", "比赛指令", "比赛预测", "今日预测",
    "今天预测", "今日持仓", "今天持仓", "比赛格式", "提交格式",
    "提交指令", "今日建议", "今天建议",
)
PNL_HINTS = ("昨天赚", "昨日收益", "昨天盈亏", "上一日收益", "昨天挣", "昨日盈利", "昨日亏损")
RUN_HINTS = (
    "测一下今日预测", "测一下今天预测", "跑一遍预测", "重新预测",
    "重新跑预测", "更新预测", "再跑一次预测", "现在跑预测", "执行今日预测",
    "帮我跑预测", "重新生成预测", "测一下今日比赛", "测一下今天比赛",
    "测一下今天", "跑今天", "跑一下今天", "现在跑一下", "现在测一下",
    "生成今日预测", "生成今天预测", "做一下今日预测", "做一下今天预测",
    "网页端跑", "网页跑", "页面跑预测", "在网页上跑", "在网页跑",
)
FORCE_RERUN_HINTS = (
    "强制重跑", "强制重新跑", "强制跑", "覆盖重跑", "覆盖再跑", "覆盖跑",
    "强制覆盖", "再跑一次也要", "硬要重跑", "确认重跑", "确定重跑",
)
# 仅查看、不重跑
VIEW_ONLY_HINTS = (
    "看看今天", "查看今天", "显示今天", "已有预测", "跑过了吗", "跑过吗",
    "不要重跑", "别重跑", "不用跑", "只看结果",
)


def _wants_run_prediction(message: str) -> bool:
    """用户明确要求「测/跑当天」→ 网页端直接执行 daily_job（不依赖 Cursor）。"""
    if any(h in message for h in VIEW_ONLY_HINTS):
        return False
    if any(h in message for h in FORCE_RERUN_HINTS):
        return True
    if any(h in message for h in RUN_HINTS):
        return True
    if any(v in message for v in ("测一下", "测试", "跑", "执行", "生成", "重跑")) and any(
        d in message for d in ("今日", "今天", "当天", "当日")
    ):
        return True
    return False
HIST_PNL_HINTS = (
    "号收益", "日收益", "号赚", "日赚", "号亏", "日亏",
    "号盈亏", "日盈亏", "号的收益", "日的收益",
    "之前赚", "之前收益", "历史收益", "前天收益", "前天赚",
)


SYSTEM_PROMPT = """你是「ETF 投资智能体」，面向用户与比赛评委。

【你能做什么】
- 解读当日比赛持仓与配置理由
- 查今日筛选后的新闻 / 解读某条新闻
- 分析「某新闻是否利好/利空某 ETF」（按四步路径）
- **查 ETF 行情**：用户问收盘价/开盘/涨跌时，系统会附带 quotes 给你
- **算今日盘后收益**：用户问今天预测涨了多少时，系统会附带「盘后收益估算」数据（仅供你组织语言，**禁止把字段名写给用户**）
- 上一交易日盈亏复盘

【工作流程】
1. **先理解用户在问什么**。
2. **再决定信息源**：
   - 涉及今日持仓/新闻/宏观/收益 → 用「当日知识库」+ 系统附带的盘后收益/行情数据（**禁止向用户提及 JSON 字段名、英文变量名**）。
   - 用户自己提供新闻让分析 → 用系统附带的 `news_screening` 客观筛选结果。
   - 与投资无关 → 礼貌引导，30 字内。
3. **再写回复**：自然口语，像投研助理在跟同事讲，**不要堆数据**。

【说话风格（重要）】
- **像人说话**：先用 1-2 句把核心结论讲出来，再展开理由。每条理由后面跟一句自己的判断，不要光罗列。
- **少用表格**：不要用 Markdown 表格输出新闻清单或行情；改用「自然段落 + 项目符号 + 一两句点评」。
- **必要数字写进句子里**，不要堆。比如别写「| 收盘 9.467 | -0.15% |」，要写「黄金 ETF 收盘 9.467，小幅回落 0.15%，整体震荡」。
- 列新闻时一条 1-2 行：标题写在前面，后面用一句话点评它对哪只 ETF 偏多/偏空，**不要列表格**。
- 信号方向不要写「中性 / 偏多」三个字交差，要带一句解释。例如：「央行 2490 亿元逆回购，量级中等，对沪深 300 影响中性」。
- 总收尾给一句**整体观点**，例如「今日资讯偏积极、券商与黄金线索更清晰」。
- 用 Markdown 但克制：可用 **粗体** 强调结论；可用 - 项目符号但每条要有解释；只在用户明确问「提交格式」「JSON」时才出代码块。

【新闻分析路径（任何"会不会涨/有没有利好"的问题都按这套走）】
① 这是不是具体事件？（订单/降准/业绩/政策落地 vs 模糊看好）
② 能否映射到某只 ETF 或板块？
③ 信号强弱与方向？（强信号 / 弱信号；偏多 / 偏空 / 中性）
④ 与当日持仓方向是否一致？若一致 → 加强；若相反 → 提醒矛盾。
然后给结论：**偏利好 / 中性 / 偏空**，**不要说"会大涨""一定赚"**。

【边界】
- 只回答 ETF / 比赛 / 新闻 / 持仓 / 宏观相关问题。
- 无关话题：30 字内引导回正题。
- **不要主动**说"我不会回答内部逻辑"。
- 只有用户**明确追问**评分公式、闸门数值、程序文件名等实现细节时，才说："这部分属于策略内部实现，不在对外问答范围。"
- 不承诺收益、不预测涨幅；用「偏多/偏空/中性」「温和看好」等。
- **严禁**在回复中出现：today_pnl、quotes、news_screening、incomplete_or_flat、data_quality、JSON 字段名等内部术语。

【输出格式】
严格输出 JSON：{"intent": <意图标签>, "reply": <Markdown 字符串>}
intent 可选：competition / why_pick / news_impact / news_list / etf_general / quote / today_pnl / pnl / off_topic / boundary_internals / greeting。
"""


def _kb_context(kb: dict[str, Any]) -> str:
    """Compress KB for the LLM prompt."""
    positions = []
    for p in kb.get("positions") or []:
        news = [
            {"title": n.get("title", ""), "url": n.get("url", ""), "direction": n.get("direction")}
            for n in (p.get("related_news") or [])[:3]
        ]
        positions.append({
            "symbol": p["symbol"],
            "symbol_name": p["symbol_name"],
            "volume": p["volume"],
            "reason": p["reason"],
            "related_news": news,
        })

    digest = []
    for n in (kb.get("news_digest") or [])[:15]:
        digest.append({
            "title": n.get("title", ""),
            "linked_etfs": [
                f"{x['symbol_name']}({x['direction']})"
                for x in (n.get("linked_etfs") or [])[:3]
            ],
        })

    econ = []
    for ev in (kb.get("econ_headlines") or [])[:8]:
        econ.append(f"{ev.get('time', '')} {ev.get('name', '')} (重要性{ev.get('importance')})")

    pool = [{"symbol": p["code"], "name": p["name"]} for p in TRADING_POOL]

    return json.dumps({
        "date": kb.get("date"),
        "is_empty_position": kb.get("is_empty_position"),
        "decision_summary_zh": kb.get("decision_summary_zh"),
        "market_context_zh": kb.get("market_context_zh"),
        "positions": positions,
        "news_digest": digest,
        "econ_headlines": econ,
        "etf_pool": pool,
    }, ensure_ascii=False)


def _format_holdings(kb: dict[str, Any] | None) -> str:
    """仅输出当日持仓（用户向，无 JSON/路径）。"""
    if not kb:
        return "暂无当日预测，请说「测一下今天」生成。"
    date = kb.get("date", "")
    out = kb.get("competition_output") or []
    if not out:
        return f"**{date}** 建议 **空仓**（不买入）。"
    lines = [f"**{date} 持仓建议**", ""]
    for p in out:
        name = p.get("symbol_name") or p.get("symbol")
        lines.append(f"- **{name}** × {int(p['volume']):,} 股")
    return "\n".join(lines)


def _format_competition(kb: dict[str, Any]) -> str:
    return _format_holdings(kb)


def _format_competition_json(kb: dict[str, Any] | None) -> str:
    """比赛提交格式：仅输出 JSON 指令。"""
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_submit = BASE_DIR / "data" / "daily_output" / f"{today_str}_submit.json"
    if today_submit.exists():
        try:
            out = json.loads(today_submit.read_text(encoding="utf-8"))
        except Exception:
            out = []
    else:
        if not kb:
            return "暂无当日预测，请先说「测一下今天」。"
        out = kb.get("competition_output") or []
    return "```json\n" + json.dumps(out, ensure_ascii=False, indent=2) + "\n```"


def _wants_competition_json(message: str) -> bool:
    hits = ("比赛预测指令", "比赛指令", "提交指令", "比赛格式", "提交格式", "json")
    return any(h in message.lower() for h in hits)


def _format_yesterday_pnl() -> str:
    """上一交易日预测的实际收益（open→close）。"""
    from daily_pnl import review_previous_prediction

    today = datetime.now().strftime("%Y-%m-%d")
    rec = review_previous_prediction(today)
    if not rec:
        return "暂无上一交易日收益记录。"
    d = rec.get("prediction_date", "")
    total = float(rec.get("total_pnl") or 0)
    rows = rec.get("positions") or []
    if not rows:
        return f"**{d}** 为空仓或无成交，收益 **{total:+.2f} 元**。"
    lines = [f"**{d}** 合计 **{total:+.2f} 元**", ""]
    for r in rows:
        name = r.get("symbol_name") or r.get("symbol")
        lines.append(f"- **{name}**：{r['return_pct']:+.2f}%，约 **{r['pnl']:+.2f} 元**")
    return "\n".join(lines)


def _format_pnl() -> str:
    return "**上一交易日收益**\n\n" + _format_yesterday_pnl()


# ---------- 历史任意日期复盘 ----------
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(s: str) -> int | None:
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十") and len(s) == 2:
        return 10 + _CN_DIGIT.get(s[1], 0)
    if s.endswith("十") and len(s) == 2:
        return _CN_DIGIT.get(s[0], 0) * 10
    if len(s) == 3 and s[1] == "十":
        return _CN_DIGIT.get(s[0], 0) * 10 + _CN_DIGIT.get(s[2], 0)
    total = 0
    for ch in s:
        if ch in _CN_DIGIT:
            total = total * 10 + _CN_DIGIT[ch]
        else:
            return None
    return total or None


def _parse_date_from_message(message: str) -> str | None:
    """从用户问题里解析出具体日期 (YYYY-MM-DD)。支持: 5/25, 5-25, 5月25日/号, 五月二十五号."""
    today = datetime.now().date()
    year = today.year

    # 1. 数字格式 5/25, 5-25, 05.25, 2026-05-25
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", message)
    if m:
        try:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        except Exception:
            pass
    m = re.search(r"(?<!\d)(\d{1,2})[\-/.月](\d{1,2})(?:日|号)?", message)
    if m:
        try:
            mo, da = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and 1 <= da <= 31:
                return f"{year:04d}-{mo:02d}-{da:02d}"
        except Exception:
            pass

    # 2. 中文格式：五月二十五号 / 五月二十五日
    m = re.search(r"([一二两三四五六七八九十零〇\d]+)月([一二两三四五六七八九十零〇\d]+)[日号]", message)
    if m:
        mo = _cn_num(m.group(1))
        da = _cn_num(m.group(2))
        if mo and da and 1 <= mo <= 12 and 1 <= da <= 31:
            return f"{year:04d}-{mo:02d}-{da:02d}"

    # 3. 相对词
    from datetime import timedelta
    if "前天" in message:
        return (today - timedelta(days=2)).strftime("%Y-%m-%d")
    if "大前天" in message:
        return (today - timedelta(days=3)).strftime("%Y-%m-%d")
    if "上周五" in message:
        d = today
        while d.weekday() != 4 or d == today:
            d -= timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    return None


def _historical_pnl(date_str: str) -> dict[str, Any] | None:
    """读取指定日期的预测档案，按当日 open→close 算每只 ETF 盈亏。"""
    p = BASE_DIR / "data" / "daily_output" / f"{date_str}_full.json"
    if not p.exists():
        return None
    try:
        full = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    picks = full.get("competition_output") or []
    if not picks:
        return {"date": date_str, "is_empty_position": True, "total_pnl": 0.0, "positions": []}

    rows = []
    total = 0.0
    for pick in picks:
        code = str(pick.get("symbol", "")).zfill(6)
        vol = int(pick.get("volume") or 0)
        bar = _load_bar(code, date_str)
        if not bar or vol <= 0:
            continue
        pnl = (bar["close"] - bar["open"]) * vol
        total += pnl
        rows.append({
            "symbol": code,
            "symbol_name": pick.get("symbol_name"),
            "volume": vol,
            "open": round(bar["open"], 4),
            "close": round(bar["close"], 4),
            "return_pct": round((bar["close"] / bar["open"] - 1) * 100, 3) if bar["open"] else 0.0,
            "pnl": round(float(pnl), 2),
        })
    return {
        "date": date_str,
        "total_pnl": round(float(total), 2),
        "settled_count": len(rows),
        "positions": rows,
    }


def _answer_history_pnl(date_str: str) -> str:
    rec = _historical_pnl(date_str)
    if rec is None:
        return (
            f"我没有找到 **{date_str}** 的预测档案。该日可能是周末/节假日，"
            "或当时还没运行过预测任务。"
        )
    if rec.get("is_empty_position"):
        return f"**{date_str}** 当日建议空仓，没有持仓，收益为 **0 元**。"
    if not rec.get("positions"):
        return f"**{date_str}** 的行情数据不完整，暂时算不出收益。"

    total = rec.get("total_pnl", 0.0)
    lines = [
        f"**{date_str} 预测持仓 · 当日盘后收益（开盘买到收盘卖）**",
        "",
        f"合计约 **{total:+.2f} 元**（50 万本金估算）。",
        "",
    ]
    for r in rec["positions"]:
        name = r.get("symbol_name") or r.get("symbol")
        lines.append(
            f"- **{name}**：开盘 {r['open']} → 收盘 {r['close']}，"
            f"涨跌 {r['return_pct']:+.2f}%，约 **{r['pnl']:+.2f} 元**"
        )
    if total > 0:
        lines.append("\n整体那天是 **小赚** 的。")
    elif total < 0:
        lines.append("\n整体那天是 **小亏** 的。")
    else:
        lines.append("\n整体那天基本持平。")
    return "\n".join(lines)


def _backup_today_outputs(date_str: str) -> None:
    """重跑前把当天 submit/full/kb 备份一份，时间戳后缀，便于回滚。"""
    import shutil
    arch = BASE_DIR / "data" / "daily_output" / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for name in (f"{date_str}_submit.json", f"{date_str}_full.json"):
        src = BASE_DIR / "data" / "daily_output" / name
        if src.exists():
            shutil.copy2(src, arch / f"{name.rsplit('.', 1)[0]}_{stamp}.json")
    kb_src = BASE_DIR / "data" / "agent_kb" / f"{date_str}.json"
    if kb_src.exists():
        kb_arch = BASE_DIR / "data" / "agent_kb" / "archive"
        kb_arch.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kb_src, kb_arch / f"{date_str}_{stamp}.json")


def _sync_knowledge_base(date_str: str) -> tuple[dict[str, Any] | None, str | None]:
    """从当日 full 输出重建 agent_kb，并写入 latest.json。"""
    try:
        kb_path = rebuild_knowledge_base(date_str)
        kb = load_knowledge_base(date_str)
        if kb is None:
            return None, f"知识库文件未生成: {kb_path}"
        return kb, None
    except Exception as exc:
        return None, str(exc)


def _run_today_prediction(skip_price_update: bool = False, *, force: bool = False) -> dict[str, Any]:
    """Run daily_job.py as a subprocess; return result + reloaded KB.

    Competition isolation:
    - Always uses official COMPETITION_CAPITAL (500000).
    - Public chat cannot --force overwrite an existing same-day run unless
      ETF_CHAT_ALLOW_FORCE_RERUN=1 is set by an operator.
    - If today's run already exists and force is blocked, returns cached KB.
    """
    from competition_guard import COMPETITION_CAPITAL, guard_chat_prediction_run
    from daily_run_guard import has_daily_run

    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        return {
            "ok": False,
            "error": "今日 A 股休市（周末），不会生成新预测。",
        }

    allowed, block_reason = guard_chat_prediction_run(force=force)
    if not allowed:
        if block_reason:
            # Explicitly blocked (e.g. force overwrite denied)
            kb = load_knowledge_base(today_str)
            if kb is None:
                kb, _ = _sync_knowledge_base(today_str)
            return {
                "ok": True,
                "skipped": True,
                "protected": True,
                "date": today_str,
                "kb": kb,
                "kb_saved": kb is not None,
                "error": block_reason,
                "message": block_reason,
            }
        # Already exists — use cache
        kb = load_knowledge_base(today_str)
        if kb is None:
            kb, _ = _sync_knowledge_base(today_str)
        return {
            "ok": True,
            "skipped": True,
            "date": today_str,
            "kb": kb,
            "kb_saved": kb is not None,
        }

    # First-run or admin-allowed force: always competition capital
    cmd = [
        sys.executable,
        str(BASE_DIR / "daily_job.py"),
        "--date", today_str,
        "--capital", str(int(COMPETITION_CAPITAL)),
    ]
    if skip_price_update:
        cmd.append("--skip-price-update")
    if force and allowed:
        # Only pass --force when guard already approved it
        cmd.append("--force")

    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("ETF_AGENT_ALLOW_NETWORK", "1")
    env.setdefault("ETF_AGENT_STRICT_DATA", "1")
    # Pin capital for child process too
    env["CAPITAL"] = str(int(COMPETITION_CAPITAL))

    try:
        proc = subprocess.run(
            cmd, cwd=str(BASE_DIR), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=360,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "预测任务超时（360s 未完成）。"}
    except Exception as exc:
        return {"ok": False, "error": f"预测任务启动失败：{exc}"}

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": f"预测任务退出码 {proc.returncode}",
            "output_tail": (proc.stdout or proc.stderr or "")[-400:],
        }

    kb, kb_error = _sync_knowledge_base(today_str)
    out: dict[str, Any] = {
        "ok": True,
        "date": today_str,
        "output_tail": (proc.stdout or "")[-400:],
        "kb": kb,
        "kb_saved": kb is not None,
    }
    if kb_error:
        out["kb_error"] = kb_error
    if kb is not None:
        out["kb_path"] = str(BASE_DIR / "data" / "agent_kb" / f"{today_str}.json")
    return out


def _format_run_result(run_result: dict[str, Any], kb: dict[str, Any] | None) -> str:
    if not run_result.get("ok"):
        return "今日预测未完成：" + str(run_result.get("error", "未知错误"))

    prefix = ""
    if run_result.get("protected"):
        prefix = (
            "今日比赛预测已受保护，对话端不会覆盖官方结果。\n"
            + str(run_result.get("message") or "")
            + "\n\n以下为已有比赛持仓：\n\n"
        )
    elif run_result.get("skipped"):
        prefix = "今日已跑过预测，直接展示已有结果（未重跑，比赛文件未改动）。\n\n"

    lines = [prefix + _format_holdings(kb), "", "**上一交易日收益**", "", _format_yesterday_pnl()]
    return "\n".join(lines)


def _is_today_pnl_question(message: str) -> bool:
    if _wants_run_prediction(message):
        return False
    hits = (
        "今天赚", "今日收益", "今天的结果", "盘后", "收盘",
        "今天涨", "今日涨", "现在闭市", "盘后收益",
        "今天预测收益", "今日预测收益",
    )
    if not any(h in message for h in hits):
        return False
    if any(h in message for h in ("昨天", "昨日", "上一日")):
        return False
    return True


def _market_closed_for_pnl() -> bool:
    """A 股日 K 落定后再报当日收益。"""
    from datetime import time as dt_time
    now = datetime.now()
    if now.weekday() >= 5:
        return True
    return now.time() >= dt_time(15, 0)


def _is_quote_question(message: str) -> bool:
    hits = ("收盘价", "开盘价", "最高价", "最低价", "今日价", "现价", "行情", "涨跌", "成交量")
    return any(h in message for h in hits)


def _extract_codes(message: str) -> list[str]:
    found: list[str] = []
    for code in re.findall(r"\b\d{6}\b", message):
        if code in POOL_BY_CODE and code not in found:
            found.append(code)
    for alias, code in sorted(ETF_ALIASES.items(), key=lambda x: -len(x[0])):
        if alias and alias in message and code not in found:
            found.append(code)
    return found


def _today_pnl_context(kb: dict[str, Any]) -> dict[str, Any] | None:
    today = kb.get("date")
    if not today:
        return None
    picks = kb.get("competition_output") or []
    if not picks:
        return {"date": today, "is_empty_position": True, "positions": [], "total_pnl": 0.0}

    rows = []
    total = 0.0
    settled = 0
    for p in picks:
        code = str(p["symbol"]).zfill(6)
        vol = int(p.get("volume") or 0)
        bar = _load_bar(code, today)
        if not bar or vol <= 0:
            continue
        if abs(bar["close"] - bar["open"]) < 1e-9:
            stale = "可能未收盘或行情未更新"
        else:
            stale = ""
        pnl = (bar["close"] - bar["open"]) * vol
        total += pnl
        settled += 1
        rows.append({
            "symbol": code,
            "symbol_name": p.get("symbol_name"),
            "volume": vol,
            "open": round(bar["open"], 4),
            "close": round(bar["close"], 4),
            "return_pct": round((bar["close"] / bar["open"] - 1) * 100, 3) if bar["open"] else 0.0,
            "pnl": round(float(pnl), 2),
            "stale_note": stale,
        })
    if settled == 0:
        return {"date": today, "data_unavailable": True}
    return {
        "date": today,
        "total_pnl": round(float(total), 2),
        "settled_count": settled,
        "positions": rows,
        "has_stale": any(r.get("stale_note") for r in rows),
    }


def _refresh_held_quotes(kb: dict[str, Any]) -> None:
    """Update CSV for held ETFs only (best effort)."""
    codes = [str(p["symbol"]).zfill(6) for p in (kb.get("competition_output") or [])]
    if not codes:
        return
    try:
        from market_data import ensure_pool_fresh
        names = {c: POOL_BY_CODE.get(c, {}).get("name", c) for c in codes}
        ensure_pool_fresh(codes, names)
    except Exception as exc:
        print(f"[agent_chat] quote refresh failed: {exc}")


def _answer_today_pnl(kb: dict[str, Any], message: str) -> str:
    """Rule-based human reply for post-market PnL — no LLM, no internal jargon."""
    if not _market_closed_for_pnl():
        return (
            "今天还没收盘，暂不报当日收益。"
            "收盘（15:00 后）再问「今天预测收益多少」。"
            "上一日收益可以说「昨天赚了多少钱」。"
        )

    cal_today = datetime.now().strftime("%Y-%m-%d")
    kb_date = kb.get("date") or cal_today
    lines: list[str] = []

    if kb_date != cal_today:
        lines.append(
            f"说明一下：当前知识库还是 **{kb_date}** 的预测，"
            f"而日历今天是 **{cal_today}**。"
        )
        lines.append(
            f"下面先按 **{kb_date}** 那次持仓统计；"
            "要看今天持仓请说「测一下今天」。"
        )
        lines.append("")

    pnl = _today_pnl_context(kb)
    if pnl and pnl.get("is_empty_position"):
        return "\n".join(lines + [
            f"**{kb_date}** 当日建议空仓，没有持仓，盘后收益为 **0 元**。"
        ])

    if pnl and pnl.get("data_unavailable"):
        lines.append(f"**{kb_date}** 行情暂不完整，稍后再问或先「测一下今天」更新。")
        return "\n".join(lines)

    if pnl and pnl.get("has_stale"):
        _refresh_held_quotes(kb)
        pnl = _today_pnl_context(kb)

    if not pnl or pnl.get("data_unavailable"):
        lines.append("行情暂不完整，请收盘后再问一次。")
        return "\n".join(lines)

    total = pnl.get("total_pnl", 0.0)
    lines.append(f"**{kb_date} 预测持仓 · 盘后收益（开盘买到收盘卖）**")
    lines.append("")
    lines.append(f"合计约 **{total:+.2f} 元**（50 万本金、按当日建议股数估算）。")
    lines.append("")

    for r in pnl.get("positions") or []:
        name = r.get("symbol_name") or r.get("symbol")
        note = f"（{r['stale_note']}）" if r.get("stale_note") else ""
        lines.append(
            f"- **{name}**：开盘 {r['open']} → 收盘 {r['close']}，"
            f"涨跌 {r['return_pct']:+.2f}%，"
            f"约 **{r['pnl']:+.2f} 元**{note}"
        )

    stale_left = [r for r in (pnl.get("positions") or []) if r.get("stale_note")]
    if stale_left:
        lines.append("")
        lines.append(f"有 {len(stale_left)} 只行情可能未更新完，收盘后再问会更准。")
    elif total > 0:
        lines.append("")
        lines.append("整体今天这次预测是 **小赚** 的。")
    elif total < 0:
        lines.append("")
        lines.append("整体今天这次预测是 **小亏** 的。")
    else:
        lines.append("")
        lines.append("整体基本持平。")

    return "\n".join(lines)


def _lookup_quotes(codes: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for code in codes:
        path = DATA_DIR / f"{str(code).zfill(6)}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if len(df) == 0:
            continue
        last = df.iloc[-1]
        date_col = "日期" if "日期" in df.columns else "date"
        col_open = "开盘" if "开盘" in df.columns else "open"
        col_close = "收盘" if "收盘" in df.columns else "close"
        col_high = "最高" if "最高" in df.columns else "high"
        col_low = "最低" if "最低" in df.columns else "low"
        col_vol = "成交量" if "成交量" in df.columns else ("volume" if "volume" in df.columns else None)
        try:
            op = float(last[col_open]); cl = float(last[col_close])
            hi = float(last[col_high]); lo = float(last[col_low])
        except Exception:
            continue
        name = POOL_BY_CODE.get(str(code).zfill(6), {}).get("name", str(code))
        out.append({
            "symbol": str(code).zfill(6),
            "symbol_name": name,
            "date": str(last[date_col])[:10],
            "open": round(op, 4),
            "close": round(cl, 4),
            "high": round(hi, 4),
            "low": round(lo, 4),
            "volume": int(last[col_vol]) if col_vol and pd.notna(last[col_vol]) else None,
            "change_pct": round((cl / op - 1) * 100, 3) if op else 0.0,
        })
    return out


def _looks_like_news_to_analyze(message: str) -> bool:
    """启发式：用户是不是在让我分析一段他给的新闻。"""
    triggers = ("利好", "利空", "受益", "影响", "会不会涨", "会涨", "大涨", "暴跌", "有没有", "怎么看")
    if not any(t in message for t in triggers):
        return False
    if len(message) < 12:
        return False
    return True


def _screen_user_news(message: str) -> dict[str, Any] | None:
    """把用户提供的新闻丢进同一套筛选器，得到客观分析结果。"""
    if not _looks_like_news_to_analyze(message):
        return None
    try:
        scored = score_news_article(
            {"title": message[:80], "content": message, "source": "user_query"}
        )
    except Exception:
        return None

    themes = scored.get("theme_scores") or {}
    direction = []
    for code, sc in sorted(themes.items(), key=lambda kv: abs(float(kv[1])), reverse=True)[:5]:
        name = next((p["name"] for p in TRADING_POOL + OFFENSIVE_POOL
                     if p["code"] == code), code)
        sc = float(sc)
        tag = "偏多" if sc >= 0.35 else ("偏空" if sc <= -0.35 else "中性")
        direction.append({
            "symbol": code, "symbol_name": name,
            "direction": tag, "strength": round(abs(sc), 3),
        })
    return {
        "accepted": scored.get("accepted", False),
        "quality": scored.get("quality", ""),
        "reason": scored.get("reason", ""),
        "event_hits": scored.get("event_hits", {}),
        "negative_hits": scored.get("negative_hits", []),
        "linked_etfs": direction,
    }


def _llm_chat(message: str, kb: dict[str, Any]) -> dict[str, Any]:
    """Use DeepSeek to answer freely, grounded on KB + optional user news screen."""
    if not llm_client.is_available():
        return _fallback_reply(message, kb)

    screen = _screen_user_news(message)
    screen_block = ""
    if screen is not None:
        screen_block = (
            "\n【用户新闻客观筛选结果（与策略内部用同一套筛选器）】\n"
            + json.dumps(screen, ensure_ascii=False) + "\n"
        )

    extras: list[str] = []
    # 盘后收益由 _answer_today_pnl 规则回复，不走 LLM

    if _is_quote_question(message):
        codes = _extract_codes(message) or [p["symbol"] for p in kb.get("positions") or []]
        if codes:
            quotes = _lookup_quotes(codes)
            if quotes:
                extras.append("【本地行情（开高低收）】\n" +
                              json.dumps(quotes, ensure_ascii=False))

    extras_block = ("\n" + "\n".join(extras) + "\n") if extras else ""

    prompt = (
        f"【当日知识库（JSON）】\n{_kb_context(kb)}\n"
        f"{screen_block}{extras_block}"
        f"\n【用户问题】\n{message}\n\n"
        "请用自然中文回答；禁止出现英文字段名或程序术语；输出 JSON。"
    )

    try:
        result = llm_client.call_json(
            prompt,
            system=SYSTEM_PROMPT,
            schema={"required": ["intent", "reply"], "types": {"intent": str, "reply": str}},
            temperature=0.35,
            max_tokens=900,
            use_cache=False,
            retries=1,
            date_tag=f"chat-{kb.get('date', '')}",
        )
        data = result.get("data") or {}
        intent = data.get("intent") or "etf_general"
        reply = data.get("reply") or ""
        return {"intent": intent, "reply": reply, "via": "llm"}
    except llm_client.LLMUnavailable as exc:
        print(f"[agent_chat] LLM unavailable: {exc}")
        return _fallback_reply(message, kb)
    except llm_client.LLMResponseError as exc:
        print(f"[agent_chat] LLM bad response: {exc}")
        return _fallback_reply(message, kb)
    except Exception as exc:
        print(f"[agent_chat] unexpected: {exc}")
        return _fallback_reply(message, kb)


# ---------------------------------------------------------------------------
# Fallback path (LLM unavailable) — minimal sensible answer.
# ---------------------------------------------------------------------------

def _fallback_reply(message: str, kb: dict[str, Any]) -> dict[str, Any]:
    msg = (message or "").strip()
    if not msg:
        return {"intent": "greeting", "reply": "请提问，例如「今日比赛预测」或「为什么买黄金 ETF」。", "via": "rule"}

    code = resolve_etf_code(msg)
    positions = kb.get("positions") or []
    if any(k in msg for k in ("为什么", "为何", "原因", "依据", "理由")):
        if code:
            for p in positions:
                if p["symbol"] == code:
                    lines = [f"#### 为什么配置 {p['symbol_name']}（{p['symbol']}）", "", p["reason"]]
                    if p.get("related_news"):
                        lines.append("\n**相关新闻：**")
                        for n in p["related_news"]:
                            lines.append(f"- [{n['title']}]({n.get('url', '')})（{n.get('direction')}）")
                    return {"intent": "why_pick", "reply": "\n".join(lines), "via": "rule"}
            return {
                "intent": "why_pick",
                "reply": f"{code} 不在今日持仓内。今日建议持仓："
                         + "、".join(p["symbol_name"] for p in positions) if positions else "今日建议空仓。",
                "via": "rule",
            }
        if positions:
            lines = ["#### 今日选股理由（汇总）", ""]
            for p in positions:
                lines.append(f"- **{p['symbol_name']}（{p['symbol']}）**：{p['reason']}")
            return {"intent": "why_pick", "reply": "\n".join(lines), "via": "rule"}

    if "新闻" in msg and ("有" in msg or "知识库" in msg or "今日" in msg or "今天" in msg):
        digest = kb.get("news_digest") or []
        lines = [f"#### {kb.get('date')} 已筛选新闻（前 10 条）", ""]
        for i, n in enumerate(digest[:10], 1):
            links = n.get("linked_etfs") or []
            tag = " · ".join(f"{x['symbol_name']}({x['direction']})" for x in links[:3])
            lines.append(f"{i}. {n.get('title', '')}{('  — ' + tag) if tag else ''}")
        return {"intent": "news_list", "reply": "\n".join(lines) or "今日暂无筛选新闻。", "via": "rule"}

    summary = kb.get("decision_summary_zh") or "当前没有可用摘要。"
    return {
        "intent": "etf_general",
        "reply": f"**{kb.get('date')} 当日观点：** {summary}\n\n你可以问：「今日比赛预测」「为什么选 512880」「今日新闻」。",
        "via": "rule",
    }


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def handle_message(message: str, date_str: str | None = None) -> dict[str, Any]:
    """Return {reply, intent, kb_date, via}."""
    message = (message or "").strip()
    if not message:
        return {"reply": "请输入您的问题。", "intent": "empty", "kb_date": None, "via": "rule"}

    target_date = (date_str or datetime.now().strftime("%Y-%m-%d"))[:10]
    kb = load_knowledge_base(target_date)

    asks_about_pnl = any(w in message for w in ("收益", "赚", "亏", "盈亏", "挣"))

    # 1. 历史任意日期收益（先于 competition / 今日收益）
    if asks_about_pnl:
        if any(h in message for h in HIST_PNL_HINTS) or "月" in message \
                or re.search(r"\d{1,2}[/\-.]\d{1,2}", message) \
                or "前天" in message or "大前天" in message or "上周" in message:
            d = _parse_date_from_message(message)
            today_str = datetime.now().strftime("%Y-%m-%d")
            if d and d != today_str:
                return {"reply": _answer_history_pnl(d), "intent": "history_pnl",
                        "kb_date": (kb or {}).get("date"), "via": "rule"}

    wants_run = _wants_run_prediction(message)

    if any(h in message for h in COMPETITION_HINTS) and not asks_about_pnl and not wants_run:
        today_kb = load_knowledge_base(datetime.now().strftime("%Y-%m-%d")) or kb
        if today_kb is None:
            return {"reply": "暂无当日预测，请说「测一下今天」。", "intent": "no_kb",
                    "kb_date": None, "via": "rule"}
        reply = _format_competition_json(today_kb) if _wants_competition_json(message) else _format_competition(today_kb)
        return {"reply": reply, "intent": "competition",
                "kb_date": today_kb.get("date"), "via": "rule"}

    if any(h in message for h in PNL_HINTS):
        return {"reply": _format_pnl(), "intent": "pnl",
                "kb_date": (kb or {}).get("date"), "via": "rule"}

    if wants_run:
        today_str = datetime.now().strftime("%Y-%m-%d")
        force = any(h in message for h in FORCE_RERUN_HINTS)
        from competition_guard import chat_force_allowed
        from daily_run_guard import has_daily_run

        today_kb = load_knowledge_base(today_str)
        if not force and (has_daily_run(today_str) or (
            today_kb and today_kb.get("date") == today_str
        )):
            if today_kb is None:
                today_kb, _ = _sync_knowledge_base(today_str)
            reply = _format_run_result({"ok": True, "skipped": True}, today_kb)
            return {
                "reply": reply,
                "intent": "run_today_cached",
                "kb_date": today_str,
                "kb_saved": True,
                "kb_updated_at": (today_kb or {}).get("updated_at"),
                "via": "rule",
            }

        # Force overwrite is blocked for public chat unless admin env is set.
        if force and has_daily_run(today_str) and not chat_force_allowed():
            if today_kb is None:
                today_kb, _ = _sync_knowledge_base(today_str)
            reply = _format_run_result(
                {
                    "ok": True,
                    "skipped": True,
                    "protected": True,
                    "message": (
                        "今日比赛预测已受保护，对话端禁止覆盖官方结果。"
                        "请使用定时任务或仪表盘重跑。"
                    ),
                },
                today_kb,
            )
            return {
                "reply": reply,
                "intent": "run_today_protected",
                "kb_date": today_str,
                "kb_saved": True,
                "via": "rule",
            }

        today_submit = BASE_DIR / "data" / "daily_output" / f"{today_str}_submit.json"
        if force and chat_force_allowed() and today_submit.exists():
            _backup_today_outputs(today_str)

        run_result = _run_today_prediction(skip_price_update=False, force=force)
        new_kb = run_result.get("kb") or load_knowledge_base(today_str)
        if run_result.get("ok") and new_kb is None:
            new_kb, kb_err = _sync_knowledge_base(today_str)
            if kb_err:
                run_result["kb_error"] = kb_err
        reply = _format_run_result(run_result, new_kb)
        intent = "run_today_protected" if run_result.get("protected") else "run_today_job"
        return {
            "reply": reply,
            "intent": intent,
            "kb_date": (new_kb or {}).get("date"),
            "kb_saved": bool(run_result.get("kb_saved") or new_kb),
            "kb_updated_at": (new_kb or {}).get("updated_at"),
            "via": "rule",
        }

    if kb is None:
        return {
            "reply": "暂无当日数据，请说「测一下今天」生成预测。",
            "intent": "no_kb", "kb_date": None, "via": "rule",
        }

    if _is_today_pnl_question(message):
        return {
            "reply": _answer_today_pnl(kb, message),
            "intent": "today_pnl",
            "kb_date": kb.get("date"),
            "via": "rule",
        }

    result = _llm_chat(message, kb)
    result["kb_date"] = kb.get("date")
    return result
