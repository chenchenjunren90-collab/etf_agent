"""Audit server file permissions and security exposure."""

from __future__ import annotations

import paramiko

from server_env import CHENJUNREN, HOST, PASSWORD, REMOTE, USER


def run(ssh, cmd: str, timeout: int = 30) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)

    sections = [
        ("USER / HOME", "whoami; id; ls -ld /home/ciyuan /home/ciyuan/chenjunren /home/ciyuan/chenjunren/etf_agent"),
        ("WORLD-WRITABLE FILES", f"find {REMOTE} -type f -perm -0002 2>/dev/null | head -30 || true"),
        ("WORLD-WRITABLE DIRS", f"find {REMOTE} -type d -perm -0002 2>/dev/null | head -30 || true"),
        ("GROUP/OTHER WRITABLE DIRS", f"find {REMOTE} -type d \\( -perm -0020 -o -perm -0002 \\) 2>/dev/null | head -20 || true"),
        (".ENV", f"ls -la {REMOTE}/.env 2>/dev/null; stat -c '%a %U:%G' {REMOTE}/.env 2>/dev/null || echo no_env"),
        ("TEAM CONFIG", f"ls -la {REMOTE}/data/team_config.json 2>/dev/null || echo no_team_config"),
        ("KEY DIRS", f"ls -ld {REMOTE}/data {REMOTE}/data/daily_output {REMOTE}/data/agent_kb {REMOTE}/.venv"),
        ("SERVICES", "systemctl show etf-dashboard etf-agent-chat -p User,Group,ExecStart --no-pager"),
        ("PROCESSES", "ps aux | grep -E 'dashboard_server|agent_server' | grep -v grep"),
        ("NGINX ETF", "grep -n etf-agent /etc/nginx/sites-enabled/default | head -15"),
        ("NGINX BLOCK", "awk 'NR>=45 && NR<=90' /etc/nginx/sites-enabled/default"),
        ("PORT8765 EXTERNAL", "curl -s -o /dev/null -w 'ext8765=%{http_code}' --connect-timeout 3 http://39.105.104.230:8765/ ; echo"),
        ("GROUPS", "groups ciyuan; getent group ciyuan"),
        ("CHAT SNIPPET", "cat /etc/nginx/snippets/etf_agent_chat.conf 2>/dev/null"),
        ("PORTS", "ss -lntp | grep -E ':80|:8765|:8766|:22'"),
        ("SSH", "grep -E '^PermitRootLogin|^PasswordAuthentication|^PubkeyAuthentication|^Port ' /etc/ssh/sshd_config 2>/dev/null"),
        ("SHELL USERS", "awk -F: '$7 ~ /sh$/ {print $1,$3,$6,$7}' /etc/passwd"),
        ("UFW", f"sudo -S ufw status 2>/dev/null <<< '{PASSWORD}' || ufw status 2>/dev/null || echo ufw_unknown"),
        ("FAIL2BAN", "systemctl is-active fail2ban 2>/dev/null || echo no_fail2ban"),
        ("HOME PERMS OTHER", "namei -l /home/ciyuan/chenjunren/etf_agent 2>/dev/null | tail -5"),
    ]

    for title, cmd in sections:
        print(f"\n=== {title} ===")
        print(run(ssh, cmd).rstrip())

    ssh.close()


if __name__ == "__main__":
    main()
