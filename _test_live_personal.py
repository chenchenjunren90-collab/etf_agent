"""Verify live personal advice runs strategy now and does not touch competition files."""

from __future__ import annotations

from pathlib import Path

from live_personal_runner import run_live_personal_advice


BASE = Path(__file__).resolve().parent
OUTPUT = BASE / "data" / "daily_output"


def main() -> None:
    before = {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in OUTPUT.glob("*_submit.json")
    }
    before_full = {
        p.name: (p.stat().st_mtime_ns, p.read_bytes())
        for p in OUTPUT.glob("*_full.json")
    }

    # Use a date that has news + kline base data locally
    advice = run_live_personal_advice(
        capital=200000,
        risk_preference="conservative",
        focus="dividend",
        date_str="2026-07-06",
        allow_news_fetch=False,  # must use on-disk news only
        use_llm=False,           # faster / offline-friendly
        save_sandbox=True,
    )
    print("ok", advice.get("ok"), "live", advice.get("live"))
    print("holdings", [h["symbol"] for h in advice.get("holdings") or []])
    print("note", (advice.get("personalization_note") or "")[:80])
    assert advice.get("ok"), advice.get("error")
    assert advice.get("live") is True
    assert advice.get("mode") == "personal_live"

    for name, (mtime, content) in before.items():
        p = OUTPUT / name
        assert p.stat().st_mtime_ns == mtime, f"submit changed: {name}"
        assert p.read_bytes() == content, f"submit content changed: {name}"
    for name, (mtime, content) in before_full.items():
        p = OUTPUT / name
        assert p.stat().st_mtime_ns == mtime, f"full changed: {name}"
        assert p.read_bytes() == content, f"full content changed: {name}"

    sandbox = advice.get("sandbox_path")
    assert sandbox and "personal_output" in sandbox, sandbox
    print("LIVE_PERSONAL_OK", sandbox)


if __name__ == "__main__":
    main()
