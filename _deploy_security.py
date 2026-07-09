"""Deploy security hardening: permissions, localhost bind, rate limits, run token."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent

FILES = [
    "security_guard.py",
    "dashboard_server.py",
    "dashboard.html",
    "agent_server.py",
    "etf-dashboard.service",
    "etf-agent-chat.service",
    "nginx_etf_agent_security.conf",
    "nginx_etf_agent_chat.conf",
]

NGINX_HTTP_PATCH = r'''
from pathlib import Path
zones = """
    limit_req_zone $binary_remote_addr zone=etf_run:10m rate=1r/m;
    limit_req_zone $binary_remote_addr zone=etf_chat:10m rate=20r/m;
"""
p = Path("/etc/nginx/nginx.conf")
text = p.read_text(encoding="utf-8")
if "zone=etf_run" in text:
    print("zones already present")
    if "rate=3r/h" in text or "rate=1r/20m" in text:
        text = text.replace("rate=3r/h", "rate=1r/m").replace("rate=1r/20m", "rate=1r/m")
        p.write_text(text, encoding="utf-8")
        print("fixed invalid hourly rate")
    else:
        print("zones ok")
else:
    marker = "http {"
    if marker not in text:
        raise SystemExit("http block not found")
    text = text.replace(marker, marker + zones, 1)
    p.write_text(text, encoding="utf-8")
    print("zones inserted")
'''

NGINX_SERVER_PATCH = r'''
from pathlib import Path
p = Path("/etc/nginx/sites-enabled/default")
text = p.read_text(encoding="utf-8")
needle = "include /etc/nginx/snippets/etf_agent_security.conf;"
if needle in text:
    print("security snippet already included")
else:
    insert = "\n\tinclude /etc/nginx/snippets/etf_agent_security.conf;\n"
    chat_needle = "include /etc/nginx/snippets/etf_agent_chat.conf;"
    if chat_needle in text:
        text = text.replace(chat_needle, insert + "\t" + chat_needle)
    else:
        marker = "location ^~ /etf-agent/"
        if marker not in text:
            raise SystemExit("etf-agent location not found")
        text = text.replace(marker, insert + "\n\t" + marker, 1)
    p.write_text(text, encoding="utf-8")
    print("security snippet included")
'''


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def sudo(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    return run(ssh, f"echo '{PASSWORD}' | sudo -S {cmd}", timeout=timeout)


def ensure_run_token(ssh: paramiko.SSHClient) -> str:
    token = secrets.token_urlsafe(24)
    cmd = (
        f"cd {REMOTE} && "
        "if grep -q '^ETF_RUN_TOKEN=' .env 2>/dev/null; then "
        "grep '^ETF_RUN_TOKEN=' .env; "
        f"else echo 'ETF_RUN_TOKEN={token}' >> .env && echo CREATED_ETF_RUN_TOKEN={token}; "
        "fi"
    )
    out = run(ssh, cmd).strip()
    if "CREATED_ETF_RUN_TOKEN=" in out:
        return out.split("CREATED_ETF_RUN_TOKEN=", 1)[1].strip()
    if "=" in out:
        return out.split("=", 1)[1].strip()
    return token


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    for name in FILES:
        sftp.put(str(LOCAL / name), f"{REMOTE}/{name}")
        print("UP", name)

    for script_name, content in [
        ("_nginx_http_patch.py", NGINX_HTTP_PATCH),
        ("_nginx_server_patch_security.py", NGINX_SERVER_PATCH),
    ]:
        with sftp.file(f"{REMOTE}/{script_name}", "w") as f:
            f.write(content)

    print("--- compile ---")
    print(
        run(
            ssh,
            f"cd {REMOTE} && python3 -m py_compile security_guard.py dashboard_server.py agent_server.py && echo COMPILE_OK",
        )
    )

    print("--- run token ---")
    token = ensure_run_token(ssh)
    print("ETF_RUN_TOKEN ready (save this for force rerun):", token)

    print("--- permissions (chenjunren only) ---")
    perm_cmds = [
        f"chmod 750 {CHENJUNREN}",
        f"find {CHENJUNREN} -type d -exec chmod 750 {{}} \\;",
        f"find {CHENJUNREN} -type f ! -perm /111 -exec chmod 640 {{}} \\;",
        f"find {CHENJUNREN} -type f -perm /111 -exec chmod 750 {{}} \\;",
        f"chmod 600 {REMOTE}/.env",
        f"ls -ld {CHENJUNREN} {REMOTE} {REMOTE}/.env",
    ]
    for cmd in perm_cmds:
        print(run(ssh, cmd).strip())

    print("--- systemd dashboard ---")
    print(sudo(ssh, f"cp {REMOTE}/etf-dashboard.service /etc/systemd/system/etf-dashboard.service"))
    print(sudo(ssh, "systemctl daemon-reload"))
    print(sudo(ssh, "systemctl enable etf-dashboard"))
    print(sudo(ssh, "systemctl restart etf-dashboard"))
    time.sleep(1.5)
    print(run(ssh, "systemctl is-active etf-dashboard; ss -lntp | grep 8765 || true"))

    print("--- systemd chat ---")
    print(sudo(ssh, f"cp {REMOTE}/etf-agent-chat.service /etc/systemd/system/etf-agent-chat.service"))
    print(sudo(ssh, "systemctl restart etf-agent-chat"))
    time.sleep(1)
    print(run(ssh, "systemctl is-active etf-agent-chat; ss -lntp | grep 8766 || true"))

    print("--- nginx rate limits ---")
    print(sudo(ssh, f"cp {REMOTE}/nginx_etf_agent_security.conf /etc/nginx/snippets/etf_agent_security.conf"))
    print(sudo(ssh, f"python3 {REMOTE}/_nginx_http_patch.py"))
    print(sudo(ssh, f"python3 {REMOTE}/_nginx_server_patch_security.py"))
    print(sudo(ssh, "nginx -t"))
    print(sudo(ssh, "systemctl reload nginx"))

    print("--- health ---")
    checks = [
        "curl -s -o /dev/null -w 'dash_local=%{http_code}\\n' http://127.0.0.1:8765/",
        "curl -s -o /dev/null -w 'dash_nginx=%{http_code}\\n' http://127.0.0.1/etf-agent/",
        "curl -s -o /dev/null -w 'chat_nginx=%{http_code}\\n' http://127.0.0.1/etf-agent/chat/",
        (
            "curl -s -X POST http://127.0.0.1:8765/api/run "
            "-H 'Content-Type: application/json' -H 'X-Real-IP: 203.0.113.99' "
            "-d '{\"stream\":false,\"force\":true}' | python3 -c "
            "\"import sys,json; d=json.load(sys.stdin); print('force_blocked', d.get('status'))\""
        ),
    ]
    for cmd in checks:
        print(run(ssh, cmd).strip())

    sftp.close()
    ssh.close()
    print("SECURITY DEPLOY DONE")


if __name__ == "__main__":
    main()
