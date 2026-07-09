"""Deploy live personal advice runner."""

from __future__ import annotations

import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent

FILES = [
    "live_personal_runner.py",
    "agent_orchestrator.py",
    "info_collector.py",
    "personalized_advisor.py",
    "agent_server.py",
    "competition_guard.py",
]


def run(ssh, cmd, timeout=90):
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    for name in FILES:
        sftp.put(str(LOCAL / name), f"{REMOTE}/{name}")
        print("UP", name)
    sftp.close()

    py = f"{REMOTE}/.venv/bin/python"
    print(run(ssh, f"cd {REMOTE} && {py} -m py_compile live_personal_runner.py agent_orchestrator.py personalized_advisor.py && echo COMPILE_OK"))
    # quick live run on server (no LLM for speed)
    print(run(ssh, f"""cd {REMOTE} && {py} - <<'PY'
from pathlib import Path
from live_personal_runner import run_live_personal_advice
out = Path('data/daily_output')
before = {{p.name: p.stat().st_mtime_ns for p in out.glob('*_submit.json')}}
a = run_live_personal_advice(capital=150000, risk_preference='aggressive', focus='growth', date_str='2026-07-06', allow_news_fetch=False, use_llm=False, save_sandbox=True)
print('live', a.get('live'), 'ok', a.get('ok'), 'holdings', [h['symbol'] for h in a.get('holdings') or []])
after = {{p.name: p.stat().st_mtime_ns for p in out.glob('*_submit.json')}}
assert before == after, 'competition files changed!'
assert a.get('live') and a.get('ok')
print('REMOTE_LIVE_OK')
PY"""))
    print(run(ssh, f"echo '{PASSWORD}' | sudo -S systemctl restart etf-agent-chat"))
    time.sleep(1.5)
    print("chat=", run(ssh, "systemctl is-active etf-agent-chat").strip())
    ssh.close()
    print("DEPLOY DONE")


if __name__ == "__main__":
    main()
