"""Server connection settings — credentials from .env only."""

from __future__ import annotations

import os
import posixpath
from pathlib import Path, PurePosixPath


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return

    for line in lines:
        value = line.strip()
        if not value or value.startswith("#") or "=" not in value:
            continue
        if value.startswith("export "):
            value = value[7:].lstrip()
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
            raw = raw[1:-1]
        os.environ.setdefault(key, raw)


ENV_FILE = Path(__file__).resolve().parent / ".env"

try:
    from dotenv import load_dotenv

    load_dotenv(ENV_FILE)
except Exception:
    load_env_file(ENV_FILE)

HOST = os.environ.get("ETF_SERVER_HOST", "39.105.104.230")
USER = os.environ.get("ETF_SERVER_USER", "ciyuan")
PASSWORD = os.environ.get("ETF_SERVER_PASSWORD", "")
REMOTE = os.environ.get("ETF_SERVER_REMOTE", "/home/ciyuan/chenjunren/etf_agent")
CHENJUNREN = os.environ.get("ETF_SERVER_CHENJUNREN", "/home/ciyuan/chenjunren")


def require_allowed_remote(remote: str = REMOTE) -> str:
    """Reject deployments outside the server's chenjunren ownership boundary."""
    root = PurePosixPath(posixpath.normpath(CHENJUNREN))
    target = PurePosixPath(posixpath.normpath(remote))
    if not target.is_absolute() or (target != root and root not in target.parents):
        raise RuntimeError(
            f"Refusing deployment outside {root}: {target}"
        )
    return str(target)
