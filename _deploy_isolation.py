"""Deploy competition isolation safeguards."""

from __future__ import annotations

import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent

FILES = [
    "competition_guard.py",
    "daily_job.py",
    "etf_agent_chat.py",
    "agent_orchestrator.py",
    "personalized_advisor.py",
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
    print(run(ssh, f"cd {REMOTE} && {py} -m py_compile competition_guard.py daily_job.py etf_agent_chat.py agent_orchestrator.py personalized_advisor.py agent_server.py && echo COMPILE_OK"))
    print(run(ssh, f"""cd {REMOTE} && {py} - <<'PY'
from competition_guard import should_write_competition_artifacts, guard_chat_prediction_run, chat_force_allowed
assert should_write_competition_artifacts(500000)
assert not should_write_competition_artifacts(200000)
assert not chat_force_allowed()
print('GUARD_OK')
PY"""))
    print(run(ssh, f"echo '{PASSWORD}' | sudo -S systemctl restart etf-agent-chat"))
    time.sleep(1.5)
    print("chat=", run(ssh, "systemctl is-active etf-agent-chat").strip())
    print("dash=", run(ssh, "systemctl is-active etf-dashboard").strip())
    # confirm cron still present
    print(run(ssh, "crontab -l 2>/dev/null | grep daily_job || true").strip())
    ssh.close()
    print("DEPLOY DONE")


if __name__ == "__main__":
    main()
