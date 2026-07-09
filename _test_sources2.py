import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

script = r"""
import os, sys, traceback
sys.path.insert(0, '/home/ciyuan/chenjunren/etf_agent')
os.environ['ETF_AGENT_ALLOW_NETWORK'] = '1'

from market_data import _fetch_yfinance, _fetch_akshare

code = '510880'
print('=== akshare detail ===')
try:
    import akshare as ak
    df = ak.fund_etf_hist_em(symbol=code, period='daily', start_date='20250701', end_date='20250710', adjust='qfq')
    print('fund_etf_hist_em', None if df is None else len(df), df.tail(2) if df is not None else '')
except Exception:
    traceback.print_exc()

print('=== yfinance ===')
try:
    df = _fetch_yfinance(code, days=30)
    if df is None:
        print('yfinance None')
    else:
        print(df.tail(3)[['date','close']].to_string(index=False))
except Exception:
    traceback.print_exc()
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = c.open_sftp()
with sftp.file("/tmp/_test_sources2.py", "w") as f:
    f.write(script)
sftp.close()
_, o, e = c.exec_command(f"cd {REMOTE} && .venv/bin/python /tmp/_test_sources2.py")
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
