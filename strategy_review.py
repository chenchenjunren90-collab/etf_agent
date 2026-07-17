"""Generate a current-code review without changing the official submission.

The review reuses the news and economic-calendar inputs captured in the
official morning output. Price features remain strictly cut off before the
decision date, just like the competition pipeline. Its output lives outside
``data/daily_output`` so no submit/history reader can mistake it for an
official prediction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from decision_snapshot import STRATEGY_VERSION, strategy_manifest


BASE_DIR = Path(__file__).resolve().parent
OFFICIAL_OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
NEWS_DIR = BASE_DIR / "data" / "daily_news_signal"
REVIEW_DIR = BASE_DIR / "data" / "strategy_reviews"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def review_path(date_str: str, *, review_dir: Path = REVIEW_DIR) -> Path:
    if not DATE_RE.fullmatch(str(date_str)):
        raise ValueError(f"invalid review date: {date_str!r}")
    return review_dir / f"{date_str}.json"


def load_current_review(
    date_str: str,
    *,
    review_dir: Path = REVIEW_DIR,
) -> dict[str, Any] | None:
    """Load only a review produced by the currently deployed strategy."""
    try:
        path = review_path(date_str, review_dir=review_dir)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("strategy_version") != STRATEGY_VERSION:
        return None
    if payload.get("official_submission_unchanged") is not True:
        return None
    return payload


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _official_inputs(
    date_str: str,
    *,
    output_dir: Path,
    news_dir: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    for path in sorted(output_dir.glob(f"{date_str}*_full.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("mode") == "fatal_fallback":
            continue
        news = payload.get("news_signal")
        econ = payload.get("econ_calendar")
        if isinstance(news, dict):
            return news, econ if isinstance(econ, dict) else None, path.name

    path = news_dir / f"{date_str}.json"
    try:
        news = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, None, None
    return (news if isinstance(news, dict) else None), None, path.name


def generate_current_strategy_review(
    date_str: str,
    *,
    capital: float = 500_000.0,
    output_dir: Path = OFFICIAL_OUTPUT_DIR,
    news_dir: Path = NEWS_DIR,
    review_dir: Path = REVIEW_DIR,
) -> dict[str, Any]:
    """Run current code against immutable morning inputs and save a review."""
    if not DATE_RE.fullmatch(str(date_str)):
        raise ValueError(f"invalid review date: {date_str!r}")

    # Loading server_env applies the same project-local .env used in production.
    import server_env  # noqa: F401
    from daily_job import to_competition_output, validate_execution_consistency
    from decision_integrity import apply_integrity_env_caps, build_integrity_context
    from econ_calendar import load_econ_payload
    from goal_state import build_goal_state
    from stability_risk import build_recent_risk_context
    from strategy import run_decision
    from trading_calendar import is_trading_day

    manifest = strategy_manifest()
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    common: dict[str, Any] = {
        "date": date_str,
        "generated_at": generated_at,
        "strategy_version": STRATEGY_VERSION,
        "git_commit": manifest.get("git_commit"),
        "official_submission_unchanged": True,
        "review_kind": "current_strategy_non_official",
        "note": "当前版本截止后复核，仅供展示，不替代比赛正式提交。",
        "market_data_cutoff": "strictly_before_decision_date",
    }

    if not is_trading_day(date_str):
        payload = {
            **common,
            "status": "market_closed",
            "competition_output": [],
            "strategy_result": None,
        }
        _atomic_write(review_path(date_str, review_dir=review_dir), payload)
        return payload

    news_signal, captured_econ, source_name = _official_inputs(
        date_str,
        output_dir=output_dir,
        news_dir=news_dir,
    )
    if news_signal is None:
        payload = {
            **common,
            "status": "unavailable",
            "reason": "official_morning_news_signal_missing",
            "competition_output": [],
            "strategy_result": None,
        }
        _atomic_write(review_path(date_str, review_dir=review_dir), payload)
        return payload

    previous_env = {
        key: os.environ.get(key)
        for key in ("ETF_AGENT_ALLOW_NETWORK", "FORCE_POSITION_CAP")
    }
    try:
        os.environ["ETF_AGENT_ALLOW_NETWORK"] = "0"
        integrity_ctx = build_integrity_context(date_str)
        recent_risk = build_recent_risk_context(
            date_str,
            capital=capital,
            output_dir=output_dir,
        )
        goal_state = build_goal_state(
            date_str,
            capital=capital,
            output_dir=output_dir,
            state_path=review_dir / "goal_window.json",
        )
        econ_payload = captured_econ or load_econ_payload(
            date_str,
            allow_live=False,
            refresh=False,
        )
        apply_integrity_env_caps(integrity_ctx)
        if not econ_payload.get("event_count", 0):
            raw_cap = os.environ.get("FORCE_POSITION_CAP", "").strip()
            try:
                current_cap = float(raw_cap) if raw_cap else 1.0
            except ValueError:
                current_cap = 1.0
            os.environ["FORCE_POSITION_CAP"] = str(min(0.50, current_cap))

        result = run_decision(
            date_str,
            capital,
            llm_decision=None,
            econ_payload=econ_payload,
            recent_risk=recent_risk,
            integrity_ctx=integrity_ctx,
            goal_state=goal_state,
            theme_signals_override=news_signal,
        )
        competition_output = to_competition_output(result)
        validate_execution_consistency(result, competition_output)
        payload = {
            **common,
            "status": "ok",
            "source_official_input": source_name,
            "decision_mode": "current_rules_with_captured_morning_inputs",
            "competition_output": competition_output,
            "strategy_result": result,
        }
    except Exception as exc:
        payload = {
            **common,
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "source_official_input": source_name,
            "competition_output": [],
            "strategy_result": None,
        }
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    _atomic_write(review_path(date_str, review_dir=review_dir), payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a non-official current strategy review")
    parser.add_argument("--date", required=True)
    parser.add_argument("--capital", type=float, default=500_000.0)
    args = parser.parse_args()
    payload = generate_current_strategy_review(args.date, capital=args.capital)
    holdings = payload.get("competition_output") or []
    print(
        "STRATEGY_REVIEW "
        f"status={payload.get('status')} date={payload.get('date')} "
        f"positions={len(holdings)} version={payload.get('strategy_version')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
