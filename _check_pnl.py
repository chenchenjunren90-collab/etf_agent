import json
import paramiko
from pathlib import Path

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=15)

def run(cmd):
    _, o, e = c.exec_command(f"cd {REMOTE} && {cmd}")
    return (o.read() + e.read()).decode("utf-8", "replace")

print("=== files ===")
print(run("ls -la data/daily_output/2026-07-0* 2>/dev/null | tail -20"))

print("=== 510880 csv tail ===")
print(run("tail -8 data/510880.csv"))

print("=== settle check ===")
script = """
import json
from pathlib import Path
from dashboard_server import _settle_prediction
from settlement_prices import get_close_to_close
from daily_pnl import review_previous_prediction

p = Path('data/daily_output/2026-07-09_full.json')
print('full exists', p.exists())
if p.exists():
    d = json.loads(p.read_text(encoding='utf-8'))
    print('competition_output', d.get('competition_output'))
    print('previous_pnl in full', d.get('previous_pnl'))
print('get_close_to_close', get_close_to_close('510880', '2026-07-09'))
print('_settle_prediction', _settle_prediction(p) if p.exists() else None)
print('review_previous_prediction', review_previous_prediction('2026-07-09'))
"""
sftp = c.open_sftp()
with sftp.file("/tmp/_check_pnl_run.py", "w") as f:
    f.write(script)
sftp.close()
print(run("python3 /tmp/_check_pnl_run.py"))

c.close()
