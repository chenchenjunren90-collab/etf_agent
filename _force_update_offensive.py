"""Update offensive-pool ETFs that are still stuck at 7/6."""

from __future__ import annotations

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

SCRIPT = r'''
from market_data import update_one_etf, latest_completed_trade_date

print("target=", latest_completed_trade_date())
for code, name in [
    ("518880", "黄金ETF"),
    ("588000", "科创50ETF"),
    ("159915", "创业板ETF"),
    ("159949", "创业板50ETF"),
]:
    r = update_one_etf(code, name, max_attempts=4)
    print(code, r)
'''


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    # ensure latest market_data
    sftp.put(
        str(__file__).replace("_force_update_offensive.py", "market_data.py"),
        f"{REMOTE}/market_data.py",
    )
    path = f"{REMOTE}/_tmp_off_update.py"
    with sftp.file(path, "w") as f:
        f.write(SCRIPT)
    _, o, e = ssh.exec_command(f"cd {REMOTE} && .venv/bin/python {path}", timeout=180)
    print(o.read().decode())
    err = e.read().decode()
    if err.strip():
        print("STDERR:", err[-500:])
    # show tails
    _, o, _ = ssh.exec_command(
        f"for c in 518880 588000 159915 159949; do echo ===$c===; tail -3 {REMOTE}/data/$c.csv; done",
        timeout=15,
    )
    print(o.read().decode())
    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
