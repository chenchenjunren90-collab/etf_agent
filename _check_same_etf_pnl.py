"""Check consecutive same-ETF PnL on server."""

from __future__ import annotations

import json

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

REMOTE_PY = r'''
import json
from pathlib import Path
base = Path("''' + "/home/ciyuan/chenjunren/etf_agent/data/daily_output" + r'''")
for p in sorted(base.glob("2026-07-*_full.json")):
    d = json.loads(p.read_text(encoding="utf-8"))
    comp = d.get("competition_output") or []
    syms = [x.get("symbol") for x in comp]
    prev = d.get("previous_pnl") or {}
    print(p.name[:10], "submit=", syms, "prev_day_pnl=", prev.get("total_pnl"), "prev_date=", prev.get("prediction_date"))
print("--- risk rows from latest ---")
latest = sorted(base.glob("2026-07-*_full.json"))[-1]
d = json.loads(latest.read_text(encoding="utf-8"))
rows = (((d.get("strategy_result") or {}).get("stability_overlay") or {}).get("recent_risk") or {}).get("rows") or []
for r in rows:
    print(r.get("date"), "pnl=", r.get("pnl"), "pos=", r.get("positions"))
'''


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    path = f"{REMOTE}/_tmp_pnl_check.py"
    with sftp.file(path, "w") as f:
        f.write(REMOTE_PY)
    _, o, e = ssh.exec_command(f"python3 {path}", timeout=40)
    print(o.read().decode())
    err = e.read().decode()
    if err.strip():
        print("ERR", err)
    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
