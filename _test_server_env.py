"""Regression tests for dependency-free server environment loading."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from server_env import load_env_file


def main() -> None:
    keys = ("ETF_TEST_ENV_PLAIN", "ETF_TEST_ENV_QUOTED", "ETF_TEST_ENV_EXPORT")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "# ignored\n"
                "ETF_TEST_ENV_PLAIN=value\n"
                'ETF_TEST_ENV_QUOTED="quoted value"\n'
                "export ETF_TEST_ENV_EXPORT='exported value'\n"
                "invalid-key=ignored\n",
                encoding="utf-8",
            )
            load_env_file(env_file)

        assert os.environ["ETF_TEST_ENV_PLAIN"] == "value"
        assert os.environ["ETF_TEST_ENV_QUOTED"] == "quoted value"
        assert os.environ["ETF_TEST_ENV_EXPORT"] == "exported value"

        os.environ["ETF_TEST_ENV_PLAIN"] = "existing"
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("ETF_TEST_ENV_PLAIN=replaced\n", encoding="utf-8")
            load_env_file(env_file)
        assert os.environ["ETF_TEST_ENV_PLAIN"] == "existing"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    print("server env fallback tests passed")


if __name__ == "__main__":
    main()
