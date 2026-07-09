import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PASSWORD, timeout=15)
cmd = f"echo '{PASSWORD}' | sudo -S bash -c 'pkill -f dashboard_server.py || true; sleep 1; systemctl restart etf-dashboard; systemctl is-active etf-dashboard'"
_, o, e = c.exec_command(cmd)
print((o.read() + e.read()).decode("utf-8", "replace"))
_, o, e = c.exec_command("pgrep -af dashboard_server || true")
print((o.read() + e.read()).decode())
c.close()
