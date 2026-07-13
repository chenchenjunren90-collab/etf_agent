"""Immutable audit snapshots for each official ETF decision."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = BASE_DIR / "data" / "decision_snapshots"
STRATEGY_VERSION = "competition-balanced-entry-v4"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _git_commit() -> str | None:
    configured = os.environ.get("ETF_GIT_COMMIT", "").strip()
    if configured:
        return configured
    deployed = BASE_DIR / "DEPLOYED_VERSION.json"
    try:
        value = str(json.loads(deployed.read_text(encoding="utf-8")).get("commit") or "").strip()
        if value:
            return value
    except Exception:
        pass
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
        goal_control_mode,
    )
    from scoring import (
        MAX_SINGLE_WEIGHT,
        SCORE_GATE,
        SHORT_RACE_POSITIVE_WEIGHT_TOTAL,
        SHORT_RACE_PRICE_WEIGHT_TOTAL,
    )
    from profitability_evidence import (
        CONSERVATIVE_EXPOSURE_CAP,
        UNCALIBRATED_EXPOSURE_CAP,
        CONSERVATIVE_PROBABILITY,
        HIGH_EXPOSURE_CAP,
        HIGH_PROBABILITY,
        ROUND_TRIP_COST,
    )

    return {
        "strategy_version": STRATEGY_VERSION,
        "git_commit": _git_commit(),
        "parameters": {
            "score_gate": SCORE_GATE,
            "short_race_positive_weight_total": SHORT_RACE_POSITIVE_WEIGHT_TOTAL,
            "short_race_price_weight_total": SHORT_RACE_PRICE_WEIGHT_TOTAL,
            "news_backtest_provenance": "strict-published-and-fetched-cutoff-v1",
            "max_single_weight": MAX_SINGLE_WEIGHT,
            "goal_window_days": _env_int("ETF_GOAL_WINDOW_DAYS", GOAL_WINDOW_DAYS),
            "goal_control_mode": goal_control_mode(),
            "goal_target_return": GOAL_TARGET_RETURN,
            "goal_protect_return": GOAL_PROTECT_RETURN,
            "goal_max_drawdown": GOAL_MAX_DRAWDOWN,
            "daily_var_budget": DAILY_VAR_BUDGET,
            "empirical_round_trip_cost": ROUND_TRIP_COST,
            "high_probability_floor": HIGH_PROBABILITY,
            "conservative_probability_floor": CONSERVATIVE_PROBABILITY,
            "high_exposure_cap": HIGH_EXPOSURE_CAP,
            "conservative_exposure_cap": CONSERVATIVE_EXPOSURE_CAP,
            "uncalibrated_exposure_cap": UNCALIBRATED_EXPOSURE_CAP,
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
