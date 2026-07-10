"""Probe alternate price sources on the server."""

from __future__ import annotations

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

SCRIPT = r'''
import os
print("TUSHARE_TOKEN set:", bool(os.environ.get("TUSHARE_TOKEN")))

# baostock
try:
    import baostock as bs
    lg = bs.login()
    print("baostock login", lg.error_code, lg.error_msg)
    rs = bs.query_history_k_data_plus(
        "sh.510300", "date,close",
        start_date="2026-07-01", end_date="2026-07-10",
        frequency="d", adjustflag="2",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    print("baostock 510300 last5:", rows[-5:] if rows else None)
    bs.logout()
except Exception as e:
    print("baostock FAIL", type(e).__name__, e)

# eastmoney direct
try:
    import requests
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": "1.510300",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": 101,
        "fqt": 1,
        "end": "20500101",
        "lmt": 5,
    }
    r = requests.get(url, params=params, timeout=15)
    print("eastmoney status", r.status_code)
    print("eastmoney body", r.text[:300])
except Exception as e:
    print("eastmoney FAIL", type(e).__name__, e)

# yfinance
try:
    import yfinance as yf
    df = yf.download("510300.SS", period="10d", progress=False, auto_adjust=True)
    print("yfinance rows", 0 if df is None else len(df))
    if df is not None and len(df):
        print("yfinance last", df.tail(3))
except Exception as e:
    print("yfinance FAIL", type(e).__name__, e)
'''


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    path = f"{REMOTE}/_tmp_alt_sources.py"
    sftp = ssh.open_sftp()
    with sftp.file(path, "w") as f:
        f.write(SCRIPT)
    _, o, e = ssh.exec_command(f"cd {REMOTE} && .venv/bin/python {path}", timeout=90)
    print(o.read().decode())
    err = e.read().decode()
    if err.strip():
        print("STDERR:", err[-600:])
    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
