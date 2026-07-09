"""Server connection settings — credentials from .env only."""

from __future__ import annotations

import os
from pathlib import Path

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
