"""Check whether yesterday's close is available on server."""

from __future__ import annotations

from datetime import datetime, timedelta

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

CODES = [
    "510300", "510050", "510500", "510330", "159338",
    "510880", "512880", "512010", "518880", "588000", "159915",
]


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)

    today = datetime.now().strftime("%Y-%m-%d")
    # yesterday weekday-ish
    y = datetime.now().date() - timedelta(days=1)
    while y.weekday() >= 5:
        y -= timedelta(days=1)
    expected = str(y)

    print(f"today={today} expected_last_bar={expected}\n")

    # CSV last dates
    for code in CODES:
        cmd = f"tail -1 {REMOTE}/data/{code}.csv 2>/dev/null | cut -d, -f1"
        _, o, _ = ssh.exec_command(cmd, timeout=10)
        last = o.read().decode().strip()
        mark = "OK" if last >= expected else "STALE"
        print(f"{code} last={last or 'MISSING':12} {mark}")

    print("\n--- live AkShare probe (510300, 510880) ---")
    probe = f"""
import traceback
from datetime import datetime
try:
    import akshare as ak
    for code in ["510300", "510880"]:
        try:
            df = ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")
            print(code, "rows=", len(df), "last=", str(df.iloc[-1]["日期"]) if "日期" in df.columns else df.iloc[-1].tolist()[:2])
        except Exception as e:
            print(code, "FAIL", type(e).__name__, str(e)[:120])
except Exception as e:
    print("import_fail", e)
    traceback.print_exc()
"""
    path = f"{REMOTE}/_tmp_ak_probe.py"
    sftp = ssh.open_sftp()
    with sftp.file(path, "w") as f:
        f.write(probe)
    _, o, e = ssh.exec_command(
        f"cd {REMOTE} && .venv/bin/python {path}",
        timeout=90,
    )
    print(o.read().decode())
    err = e.read().decode()
    if err.strip():
        print("STDERR:", err[-500:])

    print("\n--- market_data target ---")
    _, o, _ = ssh.exec_command(
        f"cd {REMOTE} && .venv/bin/python -c \"from market_data import latest_completed_trade_date; print(latest_completed_trade_date())\"",
        timeout=20,
    )
    print("latest_completed_trade_date=", o.read().decode().strip())

    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
