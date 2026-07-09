import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=15)

script = r"""
import sys, json
sys.path.insert(0, '/home/ciyuan/chenjunren/etf_agent')
from pathlib import Path
from dashboard_server import _settle_prediction
from settlement_prices import get_close_to_close
from daily_pnl import review_previous_prediction

for d in ('2026-07-08', '2026-07-09'):
    print('---', d, '---')
    print('close_to_close', get_close_to_close('510880', d))
    p = Path(f'data/daily_output/{d}_full.json')
    print('_settle_prediction', _settle_prediction(p))
print('review_previous_prediction(2026-07-09)', review_previous_prediction('2026-07-09'))
"""
sftp = c.open_sftp()
with sftp.file("/tmp/_check_pnl2.py", "w") as f:
    f.write(script)
sftp.close()
_, o, e = c.exec_command(f"cd {REMOTE} && python3 /tmp/_check_pnl2.py")
print((o.read() + e.read()).decode())
c.close()
