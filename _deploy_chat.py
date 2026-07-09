"""Upload chat agent files and configure nginx + systemd on the server."""

from __future__ import annotations

import time
from pathlib import Path

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
LOCAL = Path(__file__).resolve().parent

FILES = [
    "session_store.py",
    "info_collector.py",
    "personalized_advisor.py",
    "agent_orchestrator.py",
    "agent_server.py",
    "nginx_etf_agent_chat.conf",
    "etf-agent-chat.service",
]

NGINX_PATCH = r'''
from pathlib import Path
p = Path("/etc/nginx/sites-enabled/default")
text = p.read_text(encoding="utf-8")
needle = "include /etc/nginx/snippets/etf_agent_chat.conf;"
if needle in text:
    print("already included")
else:
    marker = "location ^~ /etf-agent/"
    if marker in text:
        i = text.find(marker)
        brace = text.find("{", i)
        depth = 0
        j = brace
        while j < len(text):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        text = text[:j] + "\n\n\tinclude /etc/nginx/snippets/etf_agent_chat.conf;\n" + text[j:]
    else:
        idx = text.rfind("}")
        text = text[:idx] + "\n\tinclude /etc/nginx/snippets/etf_agent_chat.conf;\n" + text[idx:]
    p.write_text(text, encoding="utf-8")
    print("include inserted")
'''


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 90) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def sudo(ssh: paramiko.SSHClient, cmd: str, timeout: int = 90) -> str:
    return run(ssh, f"echo '{PASSWORD}' | sudo -S {cmd}", timeout=timeout)


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    for name in FILES:
        src = LOCAL / name
        dst = f"{REMOTE}/{name}"
        sftp.put(str(src), dst)
        print("UP", name)

    # helper patch script
    patch_path = f"{REMOTE}/_nginx_patch_chat.py"
    with sftp.file(patch_path, "w") as f:
        f.write(NGINX_PATCH)

    print("--- compile ---")
    print(run(ssh, f"cd {REMOTE} && python3 -m py_compile session_store.py info_collector.py personalized_advisor.py agent_orchestrator.py agent_server.py && echo COMPILE_OK"))

    print("--- systemd ---")
    print(sudo(ssh, f"cp {REMOTE}/etf-agent-chat.service /etc/systemd/system/etf-agent-chat.service"))
    print(sudo(ssh, "systemctl daemon-reload"))
    print(sudo(ssh, "systemctl enable etf-agent-chat"))
    print(sudo(ssh, "systemctl restart etf-agent-chat"))
    time.sleep(1.5)
    print(run(ssh, "systemctl is-active etf-agent-chat; ss -lntp | grep 8766 || true"))

    print("--- nginx ---")
    print(sudo(ssh, f"cp {REMOTE}/nginx_etf_agent_chat.conf /etc/nginx/snippets/etf_agent_chat.conf"))
    print(sudo(ssh, f"python3 {patch_path}"))
    print(sudo(ssh, "nginx -t"))
    print(sudo(ssh, "systemctl reload nginx"))

    print("--- health ---")
    print("local8766=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8766/").strip())
    print("nginx_chat=", run(ssh, "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/etf-agent/chat/").strip())
    print(run(ssh, "curl -s -X POST http://127.0.0.1:8766/api/session/start -H 'Content-Type: application/json' -d '{}' | python3 -c \"import sys,json;d=json.load(sys.stdin);print(d.get('intent'), d.get('session',{}).get('session_id','')[:8])\"").strip())

    sftp.close()
    ssh.close()
    print("DEPLOY DONE")
    print("Public URL: http://39.105.104.230/etf-agent/chat/")


if __name__ == "__main__":
    main()
