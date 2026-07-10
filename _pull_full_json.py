"""Pull all daily_output full.json from server for LLM-trace reuse."""

from __future__ import annotations

from pathlib import Path

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

LOCAL = Path(__file__).resolve().parent / "data" / "daily_output"


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()
    LOCAL.mkdir(parents=True, exist_ok=True)

    tar_remote = f"{REMOTE}/data/_full_json_sync.tgz"
    print("packing full.json on server...")
    _, o, _ = ssh.exec_command(
        f"cd {REMOTE}/data/daily_output && tar czf {REMOTE}/data/_full_json_sync.tgz *_full.json 2>/dev/null; ls -lh {REMOTE}/data/_full_json_sync.tgz",
        timeout=120,
    )
    print(o.read().decode().strip())
    tar_local = Path(__file__).resolve().parent / "data" / "_full_json_sync.tgz"
    sftp.get(tar_remote, str(tar_local))
    import tarfile

    with tarfile.open(tar_local, "r:gz") as tf:
        tf.extractall(LOCAL)
    files = sorted(LOCAL.glob("*_full.json"))
    print(f"full.json count: {len(files)}")
    sftp.close()
    ssh.close()


if __name__ == "__main__":
    main()
