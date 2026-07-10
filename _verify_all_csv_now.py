"""Verify all ETF CSV last dates on server."""

from __future__ import annotations

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

CODES = [
    "510300", "510050", "510500", "510330", "159338",
    "510880", "512880", "512010",
    "518880", "588000", "159915", "159949",
]


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

    _, o, _ = ssh.exec_command(
        f"cd {REMOTE} && .venv/bin/python -c \"from market_data import latest_completed_trade_date; print(latest_completed_trade_date())\"",
        timeout=20,
    )
    target = o.read().decode().strip()
    print(f"expected_completed_bar = {target}\n")

    all_ok = True
    for code in CODES:
        _, o, _ = ssh.exec_command(
            f"tail -1 {REMOTE}/data/{code}.csv 2>/dev/null | cut -d, -f1",
            timeout=10,
        )
        last = o.read().decode().strip() or "MISSING"
        # Need at least yesterday (7/9). Having 7/10 intraday is fine.
        ok = last != "MISSING" and last >= target
        if not ok:
            all_ok = False
        print(f"{code}  last={last:12}  {'OK' if ok else 'STALE/MISSING'}")

    print("\nALL_FRESH" if all_ok else "\nSOME_STALE")

    # show last 3 lines for gold specifically
    print("\n--- 518880 (黄金) last 3 ---")
    _, o, _ = ssh.exec_command(f"tail -3 {REMOTE}/data/518880.csv", timeout=10)
    print(o.read().decode())

    ssh.close()


if __name__ == "__main__":
    main()
