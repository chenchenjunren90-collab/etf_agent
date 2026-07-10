"""Download and inspect full.json for specific dates."""

from __future__ import annotations

import json
from pathlib import Path

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

DATES = ["2026-07-06", "2026-07-08", "2026-07-09", "2026-07-10"]
OUT = Path(__file__).resolve().parent / "data" / "daily_output"


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    OUT.mkdir(parents=True, exist_ok=True)

    for d in DATES:
        path = f"{REMOTE}/data/daily_output/{d}_full.json"
        _, o, _ = ssh.exec_command(f"cat {path}", timeout=30)
        raw = o.read().decode()
        local = OUT / f"{d}_full.json"
        local.write_text(raw, encoding="utf-8")
        data = json.loads(raw)
        print(f"\n{'='*60}\n{d} keys:", sorted(data.keys())[:20])
        ranked = data.get("ranked") or []
        held = data.get("summary", {}).get("held_stocks") or []
        comp = data.get("competition_output") or []
        print("held:", [(h.get("code") or h.get("symbol"), h.get("score")) for h in held])
        print("competition:", comp)
        if ranked:
            print("ranked top5:", [(x.get("code"), x.get("score"), x.get("name")) for x in ranked[:5]])
        llm = data.get("llm_trace") or {}
        print("llm cash:", llm.get("cash_decision"), "ratio:", llm.get("position_ratio_hint"))
        print("llm summary:", (llm.get("summary_zh") or "")[:120])
        views = llm.get("per_etf_view") or []
        if views:
            top_views = sorted(views, key=lambda x: float(x.get("score") or 0), reverse=True)[:5]
            print("llm top views:", [(v.get("symbol"), v.get("score"), (v.get("reason") or "")[:40]) for v in top_views])
        stab = data.get("stability_overlay") or {}
        if stab:
            print("stability:", {k: stab.get(k) for k in ("max_positions_cap", "top_score", "weak_signal", "final_invest_ratio", "notes")})
        print("market_reason:", (data.get("market_reason") or "")[:200])
        print("reasoning:", (data.get("reasoning") or "")[:250])

    ssh.close()


if __name__ == "__main__":
    main()
