"""Pull latest ETF CSVs from server for local backtest."""

from __future__ import annotations

from pathlib import Path

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

LOCAL = Path(__file__).resolve().parent / "data"
CODES = [
    "510300", "510050", "510500", "510330", "159338",
    "510880", "512880", "512010", "518880", "588000", "159915", "159949",
]


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    LOCAL.mkdir(parents=True, exist_ok=True)
    for code in CODES:
        src = f"{REMOTE}/data/{code}.csv"
        dst = LOCAL / f"{code}.csv"
        sftp.get(src, str(dst))
        # show last date
        last = dst.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1].split(",")[0]
        print(f"DL {code} last={last}")
    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
