import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=20)

for cmd in [
    "pgrep -af post_close_sync || echo none",
    f"tail -15 {REMOTE}/data/daily_output/post_close_sync.log",
    f"tail -2 {REMOTE}/data/510880.csv",
    "curl -s http://127.0.0.1:8765/api/status | python3 -c \"import sys,json; d=json.load(sys.stdin); print(json.dumps(d.get('previous_pnl'),ensure_ascii=False,indent=2))\"",
]:
    print(">>", cmd[:80])
    _, o, e = c.exec_command(cmd)
    print((o.read() + e.read()).decode("utf-8", "replace"))

c.close()
