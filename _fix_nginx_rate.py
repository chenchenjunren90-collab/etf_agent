"""Fix invalid nginx rate limit and reload."""

from __future__ import annotations

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

FIX = r'''
from pathlib import Path
p = Path("/etc/nginx/nginx.conf")
text = p.read_text(encoding="utf-8")
text = text.replace("rate=3r/h", "rate=1r/m")
text = text.replace("rate=1r/20m", "rate=1r/m")
if "zone=etf_run" not in text:
    text = text.replace(
        "http {",
        "http {\n    limit_req_zone $binary_remote_addr zone=etf_run:10m rate=1r/m;\n"
        "    limit_req_zone $binary_remote_addr zone=etf_chat:10m rate=20r/m;\n",
        1,
    )
p.write_text(text, encoding="utf-8")
print("fixed")
'''


def run(ssh, cmd, timeout=60):
    _, o, e = ssh.exec_command(cmd, timeout=timeout)
    return (o.read() + e.read()).decode()


def sudo(ssh, cmd, timeout=60):
    return run(ssh, f"echo '{PASSWORD}' | sudo -S {cmd}", timeout=timeout)


def main():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    with ssh.open_sftp().file(f"{REMOTE}/_fix_nginx_rate.py", "w") as f:
        f.write(FIX)
    print(sudo(ssh, f"python3 {REMOTE}/_fix_nginx_rate.py"))
    print(sudo(ssh, "nginx -t"))
    print(sudo(ssh, "systemctl reload nginx"))
    print(run(ssh, "curl -s -o /dev/null -w 'nginx=%{http_code}\\n' http://127.0.0.1/etf-agent/"))
    ssh.close()


if __name__ == "__main__":
    main()
