"""Immutable audit snapshots for each official ETF decision."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = BASE_DIR / "data" / "decision_snapshots"
STRATEGY_VERSION = "ten-day-profitability-v1"


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=BASE_DIR, text=True, timeout=5
        ).strip()
    except Exception:
        return None


def strategy_manifest() -> dict[str, Any]:
    from goal_state import (
        DAILY_VAR_BUDGET,
        GOAL_MAX_DRAWDOWN,
        GOAL_PROTECT_RETURN,
        GOAL_TARGET_RETURN,
        GOAL_WINDOW_DAYS,
    )
    from scoring import MAX_SINGLE_WEIGHT, SCORE_GATE

    return {
        "strategy_version": STRATEGY_VERSION,
        "git_commit": _git_commit(),
        "parameters": {
            "score_gate": SCORE_GATE,
            "max_single_weight": MAX_SINGLE_WEIGHT,
            "goal_window_days": GOAL_WINDOW_DAYS,
            "goal_target_return": GOAL_TARGET_RETURN,
            "goal_protect_return": GOAL_PROTECT_RETURN,
            "goal_max_drawdown": GOAL_MAX_DRAWDOWN,
            "daily_var_budget": DAILY_VAR_BUDGET,
        },
    }


def write_immutable_snapshot(
    date_str: str,
    payload: dict[str, Any],
    *,
    snapshot_dir: Path = SNAPSHOT_DIR,
) -> dict[str, Any]:
    """Write a content-addressed snapshot without overwriting prior runs."""
    captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    document = {
        "captured_at": captured_at,
        "manifest": strategy_manifest(),
        "payload": payload,
    }
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    day_dir = snapshot_dir / date_str
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{digest}.json"
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
    except FileExistsError:
        pass
    try:
        display_path = str(path.relative_to(BASE_DIR)).replace("\\", "/")
    except ValueError:
        display_path = str(path)
    return {
        "strategy_version": STRATEGY_VERSION,
        "git_commit": document["manifest"]["git_commit"],
        "captured_at": captured_at,
        "sha256": digest,
        "path": display_path,
    }
