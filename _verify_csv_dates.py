"""Verify CSV has 7/9 close and whether 7/10 is a premature bar."""

from __future__ import annotations

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

    cmd = f"""
cd {REMOTE}
for code in 510300 510880 512880 512010 518880 588000 159915; do
  echo "=== $code ==="
  tail -5 data/$code.csv
done
echo "=== now ==="
date
.venv/bin/python -c "from market_data import latest_completed_trade_date, is_fresh, csv_last_date; print('target', latest_completed_trade_date()); print('510300', csv_last_date('510300')); print('510880', csv_last_date('510880'))"
"""
    _, o, e = ssh.exec_command(cmd, timeout=30)
    print(o.read().decode())
    err = e.read().decode()
    if err.strip():
        print("ERR", err[-300:])
    ssh.close()


if __name__ == "__main__":
    main()
