import json
import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=20)

print("=== running post_close_sync ===")
_, o, e = c.exec_command(
    f"cd {REMOTE} && export ETF_AGENT_ALLOW_NETWORK=1 && .venv/bin/python post_close_sync.py"
)
print((o.read() + e.read()).decode("utf-8", "replace"))

verify = """
import json, sys
sys.path.insert(0, '/home/ciyuan/chenjunren/etf_agent')
from pathlib import Path
from dashboard_server import _settle_prediction
p = Path('/home/ciyuan/chenjunren/etf_agent/data/daily_output/2026-07-09_full.json')
print(json.dumps(_settle_prediction(p), ensure_ascii=False, indent=2))
"""
sftp = c.open_sftp()
with sftp.file("/tmp/_run_pnl_verify.py", "w") as f:
    f.write(verify)
sftp.close()

print("=== PnL ===")
_, o, e = c.exec_command(f"cd {REMOTE} && .venv/bin/python /tmp/_run_pnl_verify.py")
print((o.read() + e.read()).decode("utf-8", "replace"))

print("=== 510880 tail ===")
_, o, e = c.exec_command(f"tail -3 {REMOTE}/data/510880.csv")
print(o.read().decode())

c.close()
