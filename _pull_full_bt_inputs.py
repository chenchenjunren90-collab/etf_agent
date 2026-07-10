"""Pull news signals + llm_cache + API key from server for full backtests."""

from __future__ import annotations

from pathlib import Path

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER

LOCAL = Path(__file__).resolve().parent
LOCAL_NEWS = LOCAL / "data" / "daily_news_signal"
LOCAL_CACHE = LOCAL / "data" / "llm_cache"


def main() -> None:
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    LOCAL_NEWS.mkdir(parents=True, exist_ok=True)
    LOCAL_CACHE.mkdir(parents=True, exist_ok=True)

    # news signals
    _, o, _ = ssh.exec_command(f"ls -1 {REMOTE}/data/daily_news_signal/*.json 2>/dev/null")
    news_files = [x.strip() for x in o.read().decode().splitlines() if x.strip()]
    n_news = 0
    for remote in news_files:
        name = remote.rsplit("/", 1)[-1]
        sftp.get(remote, str(LOCAL_NEWS / name))
        n_news += 1
    print(f"news signals: {n_news}")

    # llm cache dirs (tar for speed)
    print("packing llm_cache on server...")
    tar_remote = f"{REMOTE}/data/_llm_cache_sync.tgz"
    _, o, e = ssh.exec_command(
        f"cd {REMOTE}/data && tar czf _llm_cache_sync.tgz llm_cache 2>/dev/null; ls -lh _llm_cache_sync.tgz",
        timeout=120,
    )
    print(o.read().decode().strip())
    tar_local = LOCAL / "data" / "_llm_cache_sync.tgz"
    sftp.get(tar_remote, str(tar_local))
    import tarfile

    with tarfile.open(tar_local, "r:gz") as tf:
        tf.extractall(LOCAL / "data")
    print(f"llm_cache extracted under {LOCAL_CACHE}")

    # ensure DEEPSEEK_API_KEY in local .env (do not print value)
    _, o, _ = ssh.exec_command(f"grep '^DEEPSEEK_API_KEY=' {REMOTE}/.env || true")
    key_line = o.read().decode().strip()
    env_path = LOCAL / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    if key_line and "DEEPSEEK_API_KEY=" not in existing:
        with env_path.open("a", encoding="utf-8") as f:
            f.write("\n" + key_line + "\n")
        print("DEEPSEEK_API_KEY appended to local .env")
    elif "DEEPSEEK_API_KEY=" in existing:
        print("DEEPSEEK_API_KEY already in local .env")
    else:
        print("WARN: no DEEPSEEK_API_KEY found on server")

    sftp.close()
    ssh.close()
    print("SYNC DONE")


if __name__ == "__main__":
    main()
