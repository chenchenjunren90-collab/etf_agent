"""Atomically deploy the locked 2026-07-23 advice and earnings guard."""

from __future__ import annotations

import posixpath
import json
import shlex
from datetime import datetime
from pathlib import Path

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER, require_allowed_remote


LOCAL = Path(__file__).resolve().parent
TARGET_DATE = "2026-07-23"
FULL_PATH = LOCAL / "data" / "daily_output" / f"{TARGET_DATE}_full.json"
FULL_PAYLOAD = json.loads(FULL_PATH.read_text(encoding="utf-8"))
SNAPSHOT_PATH = str((FULL_PAYLOAD.get("decision_snapshot") or {}).get("path") or "")
if not SNAPSHOT_PATH:
    raise RuntimeError(f"missing decision snapshot in {FULL_PATH}")

FILES = [
    f"data/daily_output/{TARGET_DATE}_submit.json",
    f"data/daily_output/{TARGET_DATE}_full.json",
    f"data/agent_kb/{TARGET_DATE}.json",
    "data/agent_kb/latest.json",
    f"data/manual_research/{TARGET_DATE}.json",
    SNAPSHOT_PATH,
]
CRON_TAG = "ETF_ALPHABET_GUARD_20260723"


def run(ssh: paramiko.SSHClient, command: str, *, check: bool = True) -> str:
    _, stdout, stderr = ssh.exec_command(command, timeout=90)
    code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    if check and code != 0:
        raise RuntimeError(f"remote command failed ({code}): {command}\n{out}{err}")
    return out + err


def main() -> int:
    remote = require_allowed_remote()
    missing = [rel for rel in FILES if not (LOCAL / rel).is_file()]
    if missing:
        raise FileNotFoundError(f"missing deployment files: {missing}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    stage = posixpath.join(remote, ".deploy", stamp)
    backup = posixpath.join(remote, "deploy_backups", stamp)
    q_remote = shlex.quote(remote)
    q_stage = shlex.quote(stage)
    q_backup = shlex.quote(backup)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    sftp = ssh.open_sftp()
    deployed = False
    cron_changed = False
    original_cron = run(ssh, "crontab -l 2>/dev/null || true", check=False)
    try:
        run(ssh, f"test -d {q_remote} && mkdir -p {q_stage} {q_backup}")
        cron_backup = posixpath.join(backup, "crontab.before")
        with sftp.open(cron_backup, "w") as handle:
            handle.write(original_cron)
        for rel in FILES:
            remote_stage = posixpath.join(stage, rel)
            run(ssh, f"mkdir -p {shlex.quote(posixpath.dirname(remote_stage))}")
            sftp.put(str(LOCAL / rel), remote_stage)

        for rel in FILES:
            destination = posixpath.join(remote, rel)
            previous = posixpath.join(backup, rel)
            staged = posixpath.join(stage, rel)
            run(
                ssh,
                " && ".join([
                    f"mkdir -p {shlex.quote(posixpath.dirname(destination))}",
                    f"mkdir -p {shlex.quote(posixpath.dirname(previous))}",
                    f"if test -f {shlex.quote(destination)}; then cp -p {shlex.quote(destination)} {shlex.quote(previous)}; fi",
                    f"mv -f {shlex.quote(staged)} {shlex.quote(destination)}",
                ]),
            )
        deployed = True

        run(ssh, f"cd {q_remote} && bash scripts/public_gateway.sh restart")
        health = run(
            ssh,
            " && ".join([
                "curl -fsS http://127.0.0.1:3004/healthz",
                (
                    "curl -fsS http://127.0.0.1:3004/etf-agent/api/status | "
                    f"{q_remote}/.venv/bin/python -c "
                    + shlex.quote(
                        "import json,sys; d=json.load(sys.stdin); "
                        f"assert d['view_date']=='{TARGET_DATE}'; "
                        "assert len(d.get('submit') or [])==5"
                    )
                ),
                "curl -fsS -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:3004/etf-agent/chat/api/session/start >/dev/null",
            ]),
        )

        cron_lines = [
            line for line in original_cron.splitlines()
            if CRON_TAG not in line
            and not (remote in line and "daily_job.py" in line)
        ]
        # The final portfolio is explicitly locked by the user. Remove the
        # temporary earnings guard so it cannot mutate the confirmed advice.
        cron_text = "\n".join(line for line in cron_lines if line.strip()) + "\n"
        cron_stage = posixpath.join(stage, "crontab.after")
        with sftp.open(cron_stage, "w") as handle:
            handle.write(cron_text)
        run(ssh, f"crontab {shlex.quote(cron_stage)}")
        cron_changed = True
        cron_check = run(
            ssh,
            f"! crontab -l 2>/dev/null | grep -q {shlex.quote(CRON_TAG)} && echo guard_removed",
        )
        print(
            f"DEPLOY OK\nremote={remote}\nbackup={backup}\n"
            f"target={TARGET_DATE}\n{health.strip()}\n{cron_check.strip()}"
        )
    except Exception:
        if cron_changed:
            run(ssh, f"crontab {shlex.quote(posixpath.join(backup, 'crontab.before'))}", check=False)
        if deployed:
            run(
                ssh,
                f"if test -d {q_backup}; then cp -a {q_backup}/. {q_remote}/; fi",
                check=False,
            )
            run(
                ssh,
                f"cd {q_remote} && bash scripts/public_gateway.sh restart",
                check=False,
            )
        raise
    finally:
        run(ssh, f"rm -rf {q_stage}", check=False)
        sftp.close()
        ssh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
