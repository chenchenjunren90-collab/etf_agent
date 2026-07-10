"""Force-update ETF CSVs via Baostock fallback and report last dates."""

from __future__ import annotations

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    # upload market_data.py first
    local = __file__.replace("_force_update_prices.py", "market_data.py")
    sftp.put(local, f"{REMOTE}/market_data.py")
    print("UP market_data.py")

    script = f"""
from update_local_csv import update_local_etfs
from market_data import latest_completed_trade_date
from pathlib import Path

print("target=", latest_completed_trade_date())
stats = update_local_etfs(log_fn=print)
print("STATS", stats)
base = Path("{REMOTE}/data")
for code in ["510300","510050","510500","510330","159338","510880","512880","512010","518880","588000","159915"]:
    p = base / f"{{code}}.csv"
    if not p.exists():
        print(code, "MISSING")
        continue
    last = p.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1].split(",")[0]
    print(code, "last=", last)
"""
    path = f"{REMOTE}/_tmp_force_update.py"
    with sftp.file(path, "w") as f:
        f.write(script)

    print("--- updating ---")
    _, o, e = ssh.exec_command(
        f"cd {REMOTE} && .venv/bin/python {path}",
        timeout=300,
    )
    print(o.read().decode())
    err = e.read().decode()
    if err.strip():
        print("STDERR:", err[-800:])

    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
