"""Upload fixed agent_server.py and restart chat service."""

from __future__ import annotations

import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent


def run(ssh, cmd, timeout=60):
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    sftp.put(str(LOCAL / "agent_server.py"), f"{REMOTE}/agent_server.py")
    sftp.close()
    print(run(ssh, f"echo '{PASSWORD}' | sudo -S systemctl restart etf-agent-chat"))
    time.sleep(1.5)
    print("active=", run(ssh, "systemctl is-active etf-agent-chat").strip())
    # verify HTML contains API_BASE
    html = run(ssh, "curl -s http://127.0.0.1:8766/ | grep -o 'API_BASE' | head -1").strip()
    print("has_API_BASE=", html)
    print("kb=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/etf-agent/chat/api/kb").strip())
    print("page=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/etf-agent/chat/").strip())
    ssh.close()
    print("DONE")


if __name__ == "__main__":
    main()
