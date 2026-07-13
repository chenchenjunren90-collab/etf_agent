"""Check recent competition predictions on server."""

from __future__ import annotations

import json

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    cmd = (
        f"ls -1t {REMOTE}/data/daily_output/*_submit.json 2>/dev/null | head -8"
    )
    _, out, _ = ssh.exec_command(cmd, timeout=30)
    files = [f.strip() for f in out.read().decode().splitlines() if f.strip()]
    for path in reversed(files):
        _, o, _ = ssh.exec_command(f"cat {path}", timeout=15)
        raw = o.read().decode()
        try:
            data = json.loads(raw)
            symbols = [x.get("symbol") for x in data]
            print(path.split("/")[-1], "->", symbols)
        except Exception as exc:
            print(path, "ERR", exc, raw[:120])

    print("\n--- full ranked top3 (last 5 days) ---")
    cmd2 = (
        f"ls -1t {REMOTE}/data/daily_output/*_full.json 2>/dev/null | head -8"
    )
    _, out2, _ = ssh.exec_command(cmd2, timeout=30)
    full_files = [f.strip() for f in out2.read().decode().splitlines() if f.strip()]
    for path in reversed(full_files[:5]):
        _, o, _ = ssh.exec_command(
            f"{REMOTE}/.venv/bin/python -c \"import json; d=json.load(open('{path}')); "
            "r=(d.get('strategy_result') or {}).get('ranked',[]); "
            f"print('{path}', [(x.get('code'), round(x.get('score',0),3)) for x in r[:5]])\"",
            timeout=15,
        )
        print(o.read().decode().strip())

    ssh.close()


if __name__ == "__main__":
    main()
