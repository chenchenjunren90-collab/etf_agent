"""Deploy post-close sync + dashboard updates to server and install cron."""
from __future__ import annotations

import paramiko
from pathlib import Path

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER, require_allowed_remote
ROOT = Path(__file__).resolve().parent
require_allowed_remote()

FILES = [
    "post_close_sync.py",
    "scripts/post_close_sync.sh",
    "dashboard_server.py",
    "dashboard.html",
]

CRON_LINES = [
    "50 7 * * 1-5 cd /home/ciyuan/chenjunren/etf_agent && /usr/bin/flock -n /tmp/etf-agent-daily.lock env ETF_TEN_DAY_GOAL_MODE=risk_cap ETF_LLM_THEME_MODE=audit ETF_ALLOW_LLM_SCORE_CONTROL=0 ETF_REPEAT_TILT=1 .venv/bin/python daily_job.py >> data/daily_job_cron.log 2>&1",
    "10 8 * * 1-5 cd /home/ciyuan/chenjunren/etf_agent && /usr/bin/flock -n /tmp/etf-agent-daily.lock env ETF_TEN_DAY_GOAL_MODE=risk_cap ETF_LLM_THEME_MODE=audit ETF_ALLOW_LLM_SCORE_CONTROL=0 ETF_REPEAT_TILT=1 .venv/bin/python daily_job.py --force --skip-price-update >> data/daily_job_cron.log 2>&1",
    "25 8 * * 1-5 cd /home/ciyuan/chenjunren/etf_agent && /usr/bin/flock -n /tmp/etf-agent-daily.lock env ETF_TEN_DAY_GOAL_MODE=risk_cap ETF_LLM_THEME_MODE=audit ETF_ALLOW_LLM_SCORE_CONTROL=0 ETF_REPEAT_TILT=1 .venv/bin/python daily_job.py --force --skip-price-update >> data/daily_job_cron.log 2>&1",
    "15 16 * * 1-5 /home/ciyuan/chenjunren/etf_agent/scripts/post_close_sync.sh >> /home/ciyuan/chenjunren/etf_agent/data/daily_output/post_close_sync.log 2>&1",
    "45 16 * * 1-5 /home/ciyuan/chenjunren/etf_agent/scripts/post_close_sync.sh >> /home/ciyuan/chenjunren/etf_agent/data/daily_output/post_close_sync.log 2>&1",
    "15 17 * * 1-5 /home/ciyuan/chenjunren/etf_agent/scripts/post_close_sync.sh >> /home/ciyuan/chenjunren/etf_agent/data/daily_output/post_close_sync.log 2>&1",
]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=20)
sftp = c.open_sftp()

for rel in FILES:
    local = ROOT / rel
    remote = f"{REMOTE}/{rel.replace(chr(92), '/')}"
    remote_dir = "/".join(remote.split("/")[:-1])
    try:
        sftp.stat(remote_dir)
    except FileNotFoundError:
        c.exec_command(f"mkdir -p {remote_dir}")
    print(f"upload {rel}")
    sftp.put(str(local), remote)

sftp.close()

_, o, e = c.exec_command(f"chmod +x {REMOTE}/scripts/post_close_sync.sh")
o.read()

cron_body = "\n".join(CRON_LINES) + "\n"
c.exec_command(f"mkdir -p {REMOTE}/.deploy")
sftp = c.open_sftp()
with sftp.file(f"{REMOTE}/.deploy/etf_cron_new", "w") as f:
    f.write(cron_body)
sftp.close()
_, o, e = c.exec_command(f"crontab {REMOTE}/.deploy/etf_cron_new && crontab -l")
print("=== crontab ===")
print(o.read().decode())

print("=== run post_close_sync now ===")
_, o, e = c.exec_command(
    f"cd {REMOTE} && export ETF_AGENT_ALLOW_NETWORK=1 && .venv/bin/python post_close_sync.py"
)
out = (o.read() + e.read()).decode("utf-8", "replace")
print(out)

print("=== restart dashboard ===")
_, o, e = c.exec_command("systemctl restart etf-dashboard && systemctl is-active etf-dashboard")
print(o.read().decode())

print("=== verify pnl ===")
script = """
import sys
sys.path.insert(0, '/home/ciyuan/chenjunren/etf_agent')
from pathlib import Path
from dashboard_server import _settle_prediction
p = Path('/home/ciyuan/chenjunren/etf_agent/data/daily_output/2026-07-09_full.json')
print(_settle_prediction(p))
"""
sftp = c.open_sftp()
with sftp.file(f"{REMOTE}/.deploy/_verify_pnl.py", "w") as f:
    f.write(script)
sftp.close()
_, o, e = c.exec_command(f"cd {REMOTE} && .venv/bin/python {REMOTE}/.deploy/_verify_pnl.py")
print(o.read().decode())

c.close()
print("done")
