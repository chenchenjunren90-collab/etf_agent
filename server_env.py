"""Server connection settings — credentials from .env only."""

from __future__ import annotations

import os
import posixpath
from pathlib import Path, PurePosixPath

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

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
