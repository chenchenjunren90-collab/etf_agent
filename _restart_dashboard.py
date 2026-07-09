import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=20)

cmds = [
    f"cd {REMOTE} && pkill -f dashboard_server.py || true",
    "sleep 2",
    f"cd {REMOTE} && nohup .venv/bin/python dashboard_server.py --host 127.0.0.1 --port 8765 --no-browser >> data/dashboard_server.log 2>&1 &",
    "sleep 2",
    "pgrep -af dashboard_server || true",
    "curl -s -o /dev/null -w 'http=%{http_code}' http://127.0.0.1:8765/api/status",
    f"cd {REMOTE} && nohup .venv/bin/python post_close_sync.py >> data/daily_output/post_close_sync.log 2>&1 & echo sync_bg_started",
]
for cmd in cmds:
    print(">>", cmd)
    _, o, e = c.exec_command(cmd)
    out = (o.read() + e.read()).decode("utf-8", "replace")
    if out.strip():
        print(out)

c.close()
