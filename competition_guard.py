"""Hard isolation between competition daily prediction and personal chat advice.

Rules (must not be weakened by chat / personalization):
1. Competition artifacts live only under data/daily_output/ and data/agent_kb/.
2. Personal advice is READ-ONLY against those artifacts; never writes them.
3. Public chat must not --force overwrite an existing same-day competition run.
4. Official competition capital is always 500_000; other capitals must not
   overwrite competition submit/full files.
5. Cron / dashboard remain the authoritative writers for competition outputs.
"""

from __future__ import annotations

import os
from pathlib import Path

from daily_run_guard import has_daily_run


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
PERSONAL_OUTPUT_DIR = BASE_DIR / "data" / "personal_output"
KB_DIR = BASE_DIR / "data" / "agent_kb"

# Official competition virtual capital — do not change casually.
COMPETITION_CAPITAL = 500_000.0
CAPITAL_TOLERANCE = 1.0

# Chat may create a first-run prediction only when none exists.
# Overwrite requires explicit admin env (dashboard/cron/ops), never public UI default.
CHAT_FORCE_ENV = "ETF_CHAT_ALLOW_FORCE_RERUN"


def is_competition_capital(capital: float | int | None) -> bool:
    if capital is None:
        return True  # default path = competition
    try:
        return abs(float(capital) - COMPETITION_CAPITAL) <= CAPITAL_TOLERANCE
    except Exception:
        return False


def chat_force_allowed() -> bool:
    return os.environ.get(CHAT_FORCE_ENV, "").strip() in ("1", "true", "TRUE", "yes")


def guard_chat_prediction_run(*, force: bool) -> tuple[bool, str | None]:
    """
    Decide whether chat is allowed to invoke daily_job.
    Returns (allowed_to_run, block_reason).
    If not allowed_to_run and reason is None → should use cached result.
    """
    from datetime import datetime

    today = datetime.now().strftime("%Y-%m-%d")
    exists = has_daily_run(today)

    if force:
        if not chat_force_allowed():
            return False, (
                "今日比赛预测已受保护，对话端禁止覆盖。"
                "如需重跑请使用定时任务或仪表盘，并确认操作。"
                f"（管理员可设 {CHAT_FORCE_ENV}=1）"
            )
        return True, None

    if exists:
        # Do not re-run; caller should load cache
        return False, None

    # First run of the day from chat is allowed (helps if cron missed),
    # but always at competition capital.
    return True, None


def competition_output_paths(date_str: str) -> dict[str, Path]:
    return {
        "submit": OUTPUT_DIR / f"{date_str}_submit.json",
        "full": OUTPUT_DIR / f"{date_str}_full.json",
        "kb": KB_DIR / f"{date_str}.json",
    }


def personal_output_paths(date_str: str) -> dict[str, Path]:
    PERSONAL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return {
        "submit": PERSONAL_OUTPUT_DIR / f"{date_str}_submit.json",
        "full": PERSONAL_OUTPUT_DIR / f"{date_str}_full.json",
    }


def should_write_competition_artifacts(capital: float) -> bool:
    """Only competition capital may write official daily_output / agent_kb."""
    return is_competition_capital(capital)
