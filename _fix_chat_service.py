"""Fix chat service to use project venv and verify health."""

from __future__ import annotations

import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 60) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def sudo(ssh: paramiko.SSHClient, cmd: str) -> str:
    return run(ssh, f"echo '{PASSWORD}' | sudo -S {cmd}")


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    sftp.put(str(LOCAL / "etf-agent-chat.service"), f"{REMOTE}/etf-agent-chat.service")
    sftp.close()

    print(sudo(ssh, f"cp {REMOTE}/etf-agent-chat.service /etc/systemd/system/etf-agent-chat.service"))
    print(sudo(ssh, "systemctl daemon-reload"))
    print(sudo(ssh, "systemctl restart etf-agent-chat"))
    time.sleep(2)
    print(run(ssh, "systemctl is-active etf-agent-chat"))
    print(run(ssh, "ss -lntp | grep 8766 || true"))
    print("local=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/").strip())
    print("nginx=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/etf-agent/chat/").strip())
    print(run(ssh, "curl -s -X POST http://127.0.0.1:8766/api/session/start -H 'Content-Type: application/json' -d '{}'").strip()[:200])
    print(run(ssh, "systemctl status etf-agent-chat --no-pager -l | head -20"))
    ssh.close()


if __name__ == "__main__":
    main()
