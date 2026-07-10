"""Diagnose why recent days pick same ETF."""

from __future__ import annotations

import json

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

DATES = ["2026-07-06", "2026-07-08", "2026-07-09", "2026-07-10"]


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

    for d in DATES:
        path = f"{REMOTE}/data/daily_output/{d}_full.json"
        _, o, e = ssh.exec_command(f"test -f {path} && cat {path} || echo MISSING", timeout=30)
        raw = o.read().decode()
        if raw.strip() == "MISSING":
            print(f"\n=== {d} MISSING ===")
            continue
        data = json.loads(raw)
        ranked = data.get("ranked") or data.get("ranked_etfs") or []
        holdings = data.get("holdings") or data.get("competition_output") or []
        summary = data.get("summary") or {}
        llm = data.get("llm_trace") or data.get("llm") or {}
        print(f"\n=== {d} ===")
        print("submit:", [h.get("symbol") for h in (data.get("competition_output") or holdings)])
        print("mode:", summary.get("mode"), "held:", summary.get("stocks_held"), "util:", summary.get("utilization_rate"))
        print("market_reason:", (data.get("market_reason") or summary.get("market_reason") or "")[:200])
        if ranked:
            top = ranked[:6]
            print("ranked top6:", [(x.get("symbol"), round(float(x.get("score", 0)), 2), x.get("name", "")[:8]) for x in top])
        else:
            print("ranked: EMPTY in full.json")
        if isinstance(llm, dict) and llm:
            print("llm keys:", list(llm.keys())[:8])
            if llm.get("error"):
                print("llm error:", llm.get("error"))
            views = llm.get("etf_views") or llm.get("views")
            if views:
                print("llm views sample:", list(views.items())[:3] if isinstance(views, dict) else views[:3])
        meta = data.get("meta") or {}
        if meta:
            print("meta:", {k: meta[k] for k in list(meta)[:6]})

    print("\n--- CSV last dates ---")
    for sym in ["510880", "512880", "510300", "518880", "512010"]:
        cmd = (
            f"tail -3 {REMOTE}/data/{sym}.csv 2>/dev/null | head -3"
        )
        _, o, _ = ssh.exec_command(cmd, timeout=15)
        print(sym, ":", o.read().decode().strip().replace("\n", " | "))

    print("\n--- daily_job log tail ---")
    _, o, _ = ssh.exec_command(
        f"tail -40 {REMOTE}/data/daily_job.log 2>/dev/null || "
        f"journalctl -u etf-dashboard --no-pager -n 20 2>/dev/null | tail -20",
        timeout=20,
    )
    print(o.read().decode())

    ssh.close()


if __name__ == "__main__":
    main()
