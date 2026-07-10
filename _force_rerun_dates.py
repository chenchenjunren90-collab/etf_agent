"""Force-rerun historical dates on server with current code."""

from __future__ import annotations

import json
import sys

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

DATES = sys.argv[1:] or ["2026-07-08", "2026-07-09"]

APP = (
    "daily_job.py",
    "strategy.py",
    "position.py",
    "decision_integrity.py",
    "features.py",
    "market_data.py",
    "scoring.py",
    "update_local_csv.py",
)


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    for name in APP:
        sftp.put(name, f"{REMOTE}/{name}")
        print("UP", name)

    for date_str in DATES:
        print(f"\n===== FORCE RERUN {date_str} =====")
        # Backup existing submit/full if present
        ssh.exec_command(
            f"cd {REMOTE}/data/daily_output && "
            f"for f in {date_str}_submit.json {date_str}_full.json; do "
            f"[ -f \"$f\" ] && cp -a \"$f\" \"${{f%.json}}_pre_rerun.json\" || true; done",
            timeout=20,
        )
        cmd = (
            f"cd {REMOTE} && set -a && . ./.env && set +a && "
            "ETF_AGENT_ALLOW_NETWORK=1 ETF_AGENT_STRICT_DATA=1 "
            f".venv/bin/python -u daily_job.py --force --allow-historical --date {date_str} "
            f"2>&1 | tee data/force_rerun_{date_str}.log | tail -120"
        )
        _, o, e = ssh.exec_command(cmd, timeout=480)
        print(o.read().decode(errors="replace"))
        err = e.read().decode(errors="replace")
        if err.strip():
            print("STDERR:", err[-800:])

        check = f"""
import json
from pathlib import Path
p = Path("data/daily_output/{date_str}_full.json")
d = json.loads(p.read_text(encoding="utf-8"))
sr = d.get("strategy_result") or {{}}
print("competition:", d.get("competition_output") or d.get("positions"))
print("market_reason:", (sr.get("market_reason") or "")[:280])
print("integrity:", sr.get("integrity_context"))
print("concentration:", sr.get("concentration_risk"))
stab = sr.get("stability_overlay") or {{}}
print("stability:", stab.get("max_positions_cap"), stab.get("notes"))
held = (sr.get("summary") or {{}}).get("held_stocks") or []
print("held:", [(h.get("code"), h.get("amount"), round(float(h.get("score") or 0),1)) for h in held])
ranked = sr.get("ranked") or []
print("top5:", [(x.get("code"), round(float(x.get("score") or 0),1)) for x in ranked[:5]])
old = Path("data/daily_output/{date_str}_submit_pre_rerun.json")
if old.exists():
    od = json.loads(old.read_text(encoding="utf-8"))
    print("OLD submit:", od if isinstance(od, list) else od.get("competition_output") or od)
"""
        tmp = f"{REMOTE}/_tmp_check_hist.py"
        with sftp.file(tmp, "w") as f:
            f.write(check)
        _, o, _ = ssh.exec_command(f"cd {REMOTE} && .venv/bin/python {tmp}", timeout=30)
        print(o.read().decode(errors="replace"))

    sftp.close()
    ssh.close()
    print("DONE", DATES)


if __name__ == "__main__":
    main()
