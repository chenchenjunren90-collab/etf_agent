"""Force-rerun today's daily_job on the server with fresh prices."""

from __future__ import annotations

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

    # Ensure latest code is present for key modules
    sftp = ssh.open_sftp()
    for name in (
        "daily_job.py",
        "strategy.py",
        "position.py",
        "decision_integrity.py",
        "features.py",
        "market_data.py",
        "scoring.py",
    ):
        local = __file__.replace("_force_rerun_today.py", name)
        sftp.put(local, f"{REMOTE}/{name}")
        print("UP", name)

    cmd = (
        f"cd {REMOTE} && "
        "set -a && . ./.env && set +a && "
        "ETF_AGENT_ALLOW_NETWORK=1 ETF_AGENT_STRICT_DATA=1 "
        ".venv/bin/python -u daily_job.py --force --date 2026-07-10 "
        "2>&1 | tee data/force_rerun_2026-07-10.log | tail -80"
    )
    print("--- force rerun ---")
    _, o, e = ssh.exec_command(cmd, timeout=420)
    print(o.read().decode(errors="replace"))
    err = e.read().decode(errors="replace")
    if err.strip():
        print("STDERR:", err[-500:])

    print("--- submit ---")
    _, o, _ = ssh.exec_command(
        f"cat {REMOTE}/data/daily_output/2026-07-10_submit.json",
        timeout=15,
    )
    print(o.read().decode(errors="replace"))

    print("--- integrity / concentration ---")
    check = r'''
import json
from pathlib import Path
p = Path("data/daily_output/2026-07-10_full.json")
d = json.loads(p.read_text(encoding="utf-8"))
sr = d.get("strategy_result") or {}
print("competition:", d.get("competition_output"))
print("market_reason:", (sr.get("market_reason") or "")[:240])
print("integrity:", sr.get("integrity_context"))
print("concentration:", sr.get("concentration_risk"))
stab = sr.get("stability_overlay") or {}
print("stability max_pos:", stab.get("max_positions_cap"), "notes:", stab.get("notes"))
held = (sr.get("summary") or {}).get("held_stocks") or []
print("held:", [(h.get("code"), h.get("amount"), h.get("score")) for h in held])
ranked = sr.get("ranked") or []
print("top5:", [(x.get("code"), x.get("score")) for x in ranked[:5]])
'''
    path = f"{REMOTE}/_tmp_check_rerun.py"
    with sftp.file(path, "w") as f:
        f.write(check)
    _, o, _ = ssh.exec_command(f"cd {REMOTE} && .venv/bin/python {path}", timeout=20)
    print(o.read().decode(errors="replace"))

    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
