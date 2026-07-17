"""Regression checks for immutable raw-news snapshots."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from theme_signal import write_immutable_news_snapshot


def test_snapshots_are_content_addressed_and_never_overwritten() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        signal = {
            "date": "2026-07-17",
            "raw_articles": [{"title": "first", "published_at": "2026-07-17 08:00:00"}],
            "theme_scores": {},
        }
        first = write_immutable_news_snapshot(signal, snapshot_dir=root)
        duplicate = write_immutable_news_snapshot(signal, snapshot_dir=root)
        assert first["sha256"] == duplicate["sha256"]
        assert len(list((root / "2026-07-17").glob("*.json"))) == 1

        changed = dict(signal)
        changed["raw_articles"] = list(signal["raw_articles"]) + [
            {"title": "second", "published_at": "2026-07-17 08:30:00"}
        ]
        second = write_immutable_news_snapshot(changed, snapshot_dir=root)
        assert first["sha256"] != second["sha256"]
        files = list((root / "2026-07-17").glob("*.json"))
        assert len(files) == 2
        documents = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        assert sorted(item["raw_article_count"] for item in documents) == [1, 2]


if __name__ == "__main__":
    test_snapshots_are_content_addressed_and_never_overwritten()
    print("NEWS SNAPSHOT TESTS OK")
