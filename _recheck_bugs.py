"""One-shot recheck of current production hot paths."""
from __future__ import annotations

import json
import os
import pathlib
import py_compile
import re

# Avoid baostock/network during unit checks (STRICT defaults to on).
os.environ["ETF_AGENT_STRICT_DATA"] = "0"
os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"

fails: list[tuple[str, str]] = []
for p in sorted(pathlib.Path(".").glob("*.py")):
    try:
        py_compile.compile(str(p), doraise=True)
    except Exception as e:
        fails.append((p.name, str(e)[:160]))
print("COMPILE_FAILS", len(fails))
for item in fails:
    print(" ", item)

from pool import ALL_POOL, OFFENSIVE_ON_THRESHOLD
from scoring import SCORE_GATE, MAX_SINGLE_WEIGHT
from theme_signal import get_theme_signals
from news_time_split import split_articles_by_post_close
from decision_integrity import audit_price_freshness
from settlement_prices import get_close_to_close
from position import apply_stability_overlay, allocate_short_race

assert OFFENSIVE_ON_THRESHOLD == 3.0
assert SCORE_GATE == 50.0
assert MAX_SINGLE_WEIGHT == 0.30
print("CONST_OK pool", len(ALL_POOL))

for d in ("2026-07-08", "2026-07-09", "2026-07-10"):
    path = pathlib.Path(f"data/daily_news_signal/{d}.json")
    if not path.exists():
        continue
    s = get_theme_signals(d)
    an = s["auto_news"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    print(
        f"SIGNAL {d}: catalyst={an['catalyst_hits']} articles={an['article_count']} "
        f"fresh_acc={raw.get('fresh_accepted_count')}"
    )

arts = [
    {"t": "fri", "published_at": "2026-07-03 16:00:00"},
    {"t": "sun17", "published_at": "2026-07-05 17:59:59"},
    {"t": "sun18", "published_at": "2026-07-05 18:00:00"},
    {"t": "mon", "published_at": "2026-07-06 08:00:00"},
    {"t": "none", "published_at": ""},
]
f, s, _ = split_articles_by_post_close(arts, "2026-07-06")
ft = [a["t"] for a in f]
st = [a["t"] for a in s]
assert ft == ["sun18", "mon"], ft
assert "fri" in st and "sun17" in st and "none" in st
print("MONDAY_SPLIT_OK", ft)

src = pathlib.Path("strategy.py").read_text(encoding="utf-8")
assert src.find("LLM per_etf_view") < src.find("stay_cash 放在重打分之后")
print("STAY_CASH_ORDER_OK")

bat = pathlib.Path("start_auto.bat").read_text(encoding="utf-8", errors="ignore")
assert "SCORE_GATE_MODE=static" in bat
assert "SCORE_GATE_MODE=dynamic" not in bat
print("BAT_STATIC_OK")

a = audit_price_freshness(
    "2026-07-10",
    codes=["510300", "999999", "888888", "777777", "666666", "555555"],
)
assert a["price_stale"] is True and a["stale_ratio"] >= 0.5
print("MISSING_STALE_OK", a["stale_ratio"])

dj = pathlib.Path("daily_job.py").read_text(encoding="utf-8")
assert "非交易日" in dj or "为周末休市" in dj
assert "stale_accepted_articles" in dj
assert "fatal_fallback" in dj
assert "is_trading_day" in dj
print("WEEKEND_FATAL_GUARD_OK")

# expected bar date must use trade calendar (not weekend-only)
from decision_integrity import expected_decision_bar_date
from news_time_split import previous_trade_date as _prev_td
assert expected_decision_bar_date("2026-07-06") == _prev_td("2026-07-06")
# Labor Day 2026: May 6 reopen → previous is Apr 30
assert expected_decision_bar_date("2026-05-06").isoformat() == "2026-04-30"
print("EXPECTED_BAR_CALENDAR_OK", expected_decision_bar_date("2026-07-06"))

# fatal fallback must respect competition capital isolation
assert "should_write_competition_artifacts" in dj
assert "personal_output_paths" in dj
assert 'return _write_fatal_fallback(args.date, exc, capital=float(args.capital))' in dj
print("FATAL_ISOLATION_OK")

dash = pathlib.Path("dashboard_server.py").read_text(encoding="utf-8")
chat = pathlib.Path("etf_agent_chat.py").read_text(encoding="utf-8")
assert 'setdefault("SCORE_GATE_MODE", "static")' in dash
assert 'setdefault("SCORE_GATE_MODE", "static")' in chat
assert 'env["CAPITAL"] = "500000"' in dash
assert 'cmd.append("--load-submit")' not in dash
print("SUBPROCESS_STATIC_OK")

ld = pathlib.Path("llm_decider.py").read_text(encoding="utf-8")
assert "stale_accepted_articles" in ld
print("LLM_NEWS_STALE_ONLY_OK")

print("SETTLE", get_close_to_close("510880", "2026-07-10"))

# LLM merge must NOT overwrite article accepted_count with ETF-judgment count
from news_llm_scorer import merge_llm_into_news_signal

base_sig = {
    "accepted_count": 5,
    "strong_count": 3,
    "weak_count": 2,
    "accepted_articles": [{"title": f"t{i}", "quality": "strong"} for i in range(5)],
    "theme_scores": {"510300": 0.2},
    "max_abs_theme": 0.2,
}
llm_fake = [
    {
        "title": "t0",
        "etf_judgments": [
            {"code": "510300", "relevance": 0.9, "sentiment": "positive", "strength": "strong"},
            {"code": "510500", "relevance": 0.8, "sentiment": "positive", "strength": "moderate"},
            {"code": "512880", "relevance": 0.7, "sentiment": "positive", "strength": "weak"},
        ],
    }
]
merged = merge_llm_into_news_signal(dict(base_sig), llm_fake)
assert merged["accepted_count"] == 5, merged["accepted_count"]
assert merged["strong_count"] == 3, merged["strong_count"]
assert merged.get("llm_accepted_count", 0) >= 1
print("LLM_COUNT_PRESERVE_OK", merged["accepted_count"], merged.get("llm_accepted_count"))

# Monday fetch window must include Friday post-close
from news_fetcher import _fetch_window_start, _before_cutoff
from datetime import datetime as _dt

win_start = _fetch_window_start("2026-07-06", "09:30")
fri_post = _dt(2026, 7, 3, 16, 0, 0)
assert win_start <= fri_post, (win_start, fri_post)
assert _before_cutoff(fri_post, "2026-07-06", "09:30")
assert not _before_cutoff(_dt(2026, 7, 6, 10, 0, 0), "2026-07-06", "09:30")
print("FETCH_WINDOW_MONDAY_OK", win_start)

# Long-gap hot cutoff also applies after holiday-like gaps (not only weekday==0)
from news_time_split import monday_hot_cutoff, previous_trade_date
# 2026-07-06 is Monday → hot cutoff Sunday 18:00
assert monday_hot_cutoff("2026-07-06") is not None
# consecutive Tue should be None
assert monday_hot_cutoff("2026-07-07") is None
print("LONG_GAP_HOT_OK")

# Stale-bar drop helper
from strategy import _drop_stale_bar_names
fake_ranked = [{"code": "159915", "score": 90}, {"code": "510300", "score": 80}]
fake_ctx = {
    "price_audit": {
        "per_code": {
            "159915": {"ok": False},
            "510300": {"ok": True},
        }
    }
}
kept = _drop_stale_bar_names(fake_ranked, fake_ctx)
assert [x["code"] for x in kept] == ["510300"], kept
print("STALE_DROP_OK")

# zero fresh_accepted_count must stay 0 (not fall through to accepted_count)
fake = {
    "fresh_accepted_count": 0,
    "accepted_count": 9,
    "auto_news": {"article_count": 0, "catalyst_hits": 0},
    "catalyst_hits": 0,
    "confidence": 0.0,
    "max_abs_theme": 0.0,
}
pathlib.Path("data/daily_news_signal/_tmp_zero_fresh.json").write_text(
    json.dumps(fake, ensure_ascii=False), encoding="utf-8"
)
# theme_signal loads by date filename YYYY-MM-DD.json — use direct unit below instead
from theme_signal import _norm_score_map  # noqa: F401
import theme_signal as ts

# monkeypatch load
old = ts._load_signal
ts._load_signal = lambda date_str=None: fake  # type: ignore
try:
    got = ts.get_theme_signals("2099-01-01")
    assert got["auto_news"]["article_count"] == 0, got["auto_news"]
    assert got["auto_news"]["catalyst_hits"] == 0
    print("ZERO_FRESH_ARTICLE_COUNT_OK")
finally:
    ts._load_signal = old
    pathlib.Path("data/daily_news_signal/_tmp_zero_fresh.json").unlink(missing_ok=True)

from trading_calendar import is_trading_day, previous_trading_day
assert not is_trading_day("2026-05-01")  # Labor Day
assert not is_trading_day("2026-05-04")
assert is_trading_day("2026-05-06")
assert previous_trading_day("2026-05-06").isoformat() == "2026-04-30"
assert not is_trading_day("2026-02-18")  # Spring Festival
print("TRADING_CALENDAR_OK")

from daily_run_guard import has_daily_run
tmp_full = pathlib.Path("data/daily_output/_tmp_fatal_full.json")
# has_daily_run uses {date}_full.json naming — use a fake date file via monkeypatch path
import daily_run_guard as drg
old_full = drg.daily_full_path
drg.daily_full_path = lambda d: pathlib.Path("data/daily_output") / f"_probe_{d}_full.json"  # type: ignore
probe = pathlib.Path("data/daily_output/_probe_2099-01-02_full.json")
probe.parent.mkdir(parents=True, exist_ok=True)
probe.write_text(json.dumps({"mode": "fatal_fallback", "strategy_result": None}), encoding="utf-8")
assert has_daily_run("2099-01-02") is False
probe.write_text(json.dumps({"mode": "competition", "strategy_result": {"summary": {}}}), encoding="utf-8")
assert has_daily_run("2099-01-02") is True
probe.unlink(missing_ok=True)
drg.daily_full_path = old_full
print("HAS_DAILY_RUN_FATAL_OK")

# LLM merge updates confidence (not leave keyword-only)
merged2 = merge_llm_into_news_signal(
    {
        "accepted_count": 2,
        "strong_count": 1,
        "confidence": 0.05,
        "market_sentiment": 0.0,
        "theme_scores": {"510300": 0.1},
        "accepted_articles": [{"title": "t0"}, {"title": "t1"}],
    },
    [
        {
            "title": "t0",
            "etf_judgments": [
                {"code": "510300", "relevance": 0.9, "sentiment": "positive", "strength": "strong"},
            ],
        }
    ],
)
assert merged2["confidence"] >= 0.2, merged2["confidence"]
print("LLM_CONFIDENCE_ALIGN_OK", merged2["confidence"])

bad = []
for path, pat in [
    ("pool.py", r"OFFENSIVE_ON_THRESHOLD\s*=\s*4"),
    ("strategy.py", r"llm_lower_ratio"),
]:
    text = pathlib.Path(path).read_text(encoding="utf-8")
    if re.search(pat, text):
        bad.append((path, pat))
print("LEFTOVER_BAD", bad)
print("ALL_CHECKS_PASSED" if not fails and not bad else "HAS_ISSUES")
