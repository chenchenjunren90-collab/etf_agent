from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from daily_job import has_successful_official_output, submission_deadline_passed


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def main() -> None:
    tz = ZoneInfo("Asia/Shanghai")
    assert not submission_deadline_passed(
        "2026-07-16", "08:30", as_of=datetime(2026, 7, 16, 8, 29, 59, tzinfo=tz)
    )
    assert submission_deadline_passed(
        "2026-07-16", "08:30", as_of=datetime(2026, 7, 16, 8, 30, 0, tzinfo=tz)
    )
    assert not submission_deadline_passed(
        "2026-07-15", "08:30", as_of=datetime(2026, 7, 16, 9, 0, 0, tzinfo=tz)
    )

    with tempfile.TemporaryDirectory() as tmp:
        output_dir = Path(tmp)
        date_str = "2026-07-16"
        submit_path = output_dir / f"{date_str}_submit.json"
        full_path = output_dir / f"{date_str}_full.json"

        write_json(submit_path, [])
        write_json(
            full_path,
            {"mode": "competition", "strategy_result": {"summary": {}}, "competition_output": []},
        )
        assert has_successful_official_output(date_str, output_dir=output_dir)

        write_json(
            full_path,
            {"mode": "fatal_fallback", "strategy_result": None, "competition_output": []},
        )
        assert not has_successful_official_output(date_str, output_dir=output_dir)

        write_json(full_path, {"mode": "competition", "strategy_result": None})
        assert not has_successful_official_output(date_str, output_dir=output_dir)

    print("DAILY DEADLINE OK")


if __name__ == "__main__":
    main()
