import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASSWORD, timeout=15)

def run(cmd):
    _, o, e = client.exec_command(cmd)
    return (o.read() + e.read()).decode("utf-8", errors="replace")

print(run("systemctl is-active etf-dashboard"))
print(run("ss -lntp | grep 8765 || netstat -lntp | grep 8765"))
print(run("curl -s -o /dev/null -w 'local_8765=%{http_code}\\n' http://127.0.0.1:8765/"))
print(run("curl -s -o /dev/null -w 'nginx_etf=%{http_code}\\n' http://127.0.0.1/etf-agent/"))
print(run("grep -n 'etf-agent' /etc/nginx/sites-enabled/* /etc/nginx/conf.d/* 2>/dev/null | head -20"))
client.close()
