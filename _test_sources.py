import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

script = r"""
import sys
sys.path.insert(0, '/home/ciyuan/chenjunren/etf_agent')
from market_data import _fetch_baostock, _fetch_akshare, save_etf_csv, csv_last_date
from strategy import TRADING_POOL

code = '510880'
for name, fn in [('akshare', lambda: _fetch_akshare(code, '20250701', '20250710')),
                 ('baostock', lambda: _fetch_baostock(code, '2025-07-01', '2025-07-10'))]:
    try:
        df = fn()
        if df is None:
            print(name, 'None')
        else:
            tail = df.tail(3)[['date','close']].astype(str).to_string(index=False)
            print(name, 'OK', '\n', tail)
    except Exception as e:
        print(name, 'ERR', e)
print('csv last', csv_last_date(code))
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = c.open_sftp()
with sftp.file("/tmp/_test_sources.py", "w") as f:
    f.write(script)
sftp.close()
_, o, e = c.exec_command(f"cd {REMOTE} && .venv/bin/python /tmp/_test_sources.py")
print((o.read() + e.read()).decode("utf-8", "replace"))
c.close()
