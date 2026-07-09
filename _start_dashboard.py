import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=15)
for cmd in [
    f"echo '{PASSWORD}' | sudo -S systemctl start etf-dashboard",
    "systemctl is-active etf-dashboard",
    "pgrep -af dashboard_server || true",
    "curl -s -o /dev/null -w 'http=%{http_code}' http://127.0.0.1:8765/",
]:
    _, o, e = c.exec_command(cmd)
    print((o.read() + e.read()).decode("utf-8", "replace").strip())
c.close()
