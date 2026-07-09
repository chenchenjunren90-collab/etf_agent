"""Deploy personalization fix to server."""

from __future__ import annotations

import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent

FILES = [
    "info_collector.py",
    "personalized_advisor.py",
    "agent_orchestrator.py",
    "agent_server.py",
]


def run(ssh, cmd, timeout=60):
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
    print(run(ssh, f"cd {REMOTE} && {py} -m py_compile info_collector.py personalized_advisor.py agent_orchestrator.py agent_server.py && echo COMPILE_OK"))
    # quick remote personalization check
    print(run(ssh, f"""cd {REMOTE} && {py} - <<'PY'
from personalized_advisor import build_personal_advice
base=dict(capital=200000, date_str='2026-07-06', allow_latest_fallback=True)
a=build_personal_advice(**base, risk_preference='conservative', focus='dividend')
b=build_personal_advice(**base, risk_preference='aggressive', focus='growth')
print('A', [h['symbol'] for h in a['holdings']])
print('B', [h['symbol'] for h in b['holdings']])
assert [h['symbol'] for h in a['holdings']] != [h['symbol'] for h in b['holdings']]
print('REMOTE_OK')
PY"""))

    print(run(ssh, f"echo '{PASSWORD}' | sudo -S systemctl restart etf-agent-chat"))
    time.sleep(1.5)
    print("active=", run(ssh, "systemctl is-active etf-agent-chat").strip())
    print("page=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/etf-agent/chat/").strip())
    ssh.close()


if __name__ == "__main__":
    main()
