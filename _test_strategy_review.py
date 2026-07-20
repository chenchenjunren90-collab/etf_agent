from __future__ import annotations

import json
import tempfile
from pathlib import Path

import strategy_review
from decision_snapshot import STRATEGY_VERSION


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        output_dir = root / "daily_output"
        news_dir = root / "daily_news_signal"
        review_dir = root / "strategy_reviews"
        output_dir.mkdir()
        news_dir.mkdir()

        official_path = output_dir / "2026-07-17_full.json"
        official_payload = {
            "date": "2026-07-17",
            "competition_output": [],
            "news_signal": {"source": "captured", "accepted_count": 3},
            "econ_calendar": {"event_count": 1},
        }
        official_path.write_text(
            json.dumps(official_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        official_before = official_path.read_bytes()

        result = {
            "date": "2026-07-17",
            "summary": {
                "held_stocks": [
                    {
                        "code": "512880",
                        "name": "证券ETF",
                        "volume": 20100,
                        "amount": 24924.0,
                        "latest_price": 1.24,
                        "weight": 5.0,
                        "score": 64.0,
                    }
                ]
            },
        }
        captured: dict = {}

        original_trading = __import__("trading_calendar").is_trading_day
        original_integrity = __import__("decision_integrity").build_integrity_context
        original_risk = __import__("stability_risk").build_recent_risk_context
        original_goal = __import__("goal_state").build_goal_state
        original_run = __import__("strategy").run_decision

        __import__("trading_calendar").is_trading_day = lambda _date: True
        __import__("decision_integrity").build_integrity_context = lambda _date: {}
        __import__("stability_risk").build_recent_risk_context = lambda *a, **k: {"rows": []}
        __import__("goal_state").build_goal_state = lambda *a, **k: {"enabled": False}

        def fake_run(*args, **kwargs):
            captured.update(kwargs)
            return result

        __import__("strategy").run_decision = fake_run
        try:
            payload = strategy_review.generate_current_strategy_review(
                "2026-07-17",
                output_dir=output_dir,
                news_dir=news_dir,
                review_dir=review_dir,
            )
        finally:
            __import__("trading_calendar").is_trading_day = original_trading
            __import__("decision_integrity").build_integrity_context = original_integrity
            __import__("stability_risk").build_recent_risk_context = original_risk
            __import__("goal_state").build_goal_state = original_goal
            __import__("strategy").run_decision = original_run

        assert payload["status"] == "ok"
        assert payload["strategy_version"] == STRATEGY_VERSION
        assert payload["official_submission_unchanged"] is True
        assert payload["competition_output"] == [
            {"symbol": "512880", "symbol_name": "证券ETF", "volume": 20100}
        ]
        assert captured["llm_decision"] is None
        assert captured["theme_signals_override"]["source"] == "captured"
        assert official_path.read_bytes() == official_before
        assert not list(output_dir.glob("*_submit.json"))
        assert len(list(output_dir.glob("*_full.json"))) == 1

        loaded = strategy_review.load_current_review(
            "2026-07-17", review_dir=review_dir
        )
        assert loaded and loaded["competition_output"][0]["symbol"] == "512880"

        review_file = review_dir / "2026-07-17.json"
        stale = json.loads(review_file.read_text(encoding="utf-8"))
        stale["strategy_version"] = "legacy-version"
        review_file.write_text(json.dumps(stale), encoding="utf-8")
        assert strategy_review.load_current_review(
            "2026-07-17", review_dir=review_dir
        ) is None

        dashboard = (Path(__file__).resolve().parent / "dashboard.html").read_text(
            encoding="utf-8"
        )
        assert "当前策略复核" not in dashboard
        assert "data.current_strategy_review" not in dashboard
        assert "比赛正式提交 · 截止前封存结果" in dashboard
        assert "JSON.stringify(submit, null, 2)" in dashboard

        current_official = dict(official_payload)
        current_official["decision_snapshot"] = {
            "strategy_version": STRATEGY_VERSION,
        }
        official_path.write_text(
            json.dumps(current_official, ensure_ascii=False),
            encoding="utf-8",
        )
        _, _, _, official_version = strategy_review._official_inputs(
            "2026-07-17",
            output_dir=output_dir,
            news_dir=news_dir,
        )
        assert official_version == STRATEGY_VERSION
        not_needed = strategy_review.generate_current_strategy_review(
            "2026-07-17",
            output_dir=output_dir,
            news_dir=news_dir,
            review_dir=review_dir,
        )
        assert not_needed["status"] == "not_needed"
        assert not_needed["competition_output"] == []
        assert not_needed["strategy_result"] is None

    print("STRATEGY REVIEW OK")


if __name__ == "__main__":
    main()
