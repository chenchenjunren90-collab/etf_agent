"""Regression checks for production scheduling and version provenance."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import types
from pathlib import Path

import decision_snapshot

try:
    import paramiko  # noqa: F401
except ModuleNotFoundError:
    sys.modules["paramiko"] = types.ModuleType("paramiko")

from _sync_to_server import run_checked, tracked_code_changes


ROOT = Path(__file__).resolve().parent


def _literal_assignment(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.name}")


def test_cron_refresh_contract() -> None:
    cron = _literal_assignment(ROOT / "_deploy_post_close.py", "CRON_LINES")[:3]
    assert len(cron) == 3
    assert "--force" not in cron[0]
    assert all("--force --skip-price-update" in line for line in cron[1:])
    assert all("/usr/bin/flock -n" in line for line in cron)
    assert all("ETF_LLM_THEME_MODE=audit" in line for line in cron)
    assert all("ETF_ALLOW_LLM_SCORE_CONTROL=0" in line for line in cron)
    assert all("ETF_REPEAT_TILT=1" in line for line in cron)
    assert all("ETF_LLM_THEME_MODE=override" not in line for line in cron)
    assert "--cutoff 07:50" in cron[0]
    assert "--cutoff 08:10" in cron[1]
    assert "--cutoff 08:25" in cron[2]
    assert all("--cutoff 09:30" not in line for line in cron)


def test_snapshot_deployment_markers() -> None:
    original = decision_snapshot.BASE_DIR
    with tempfile.TemporaryDirectory() as temp_dir:
        base = Path(temp_dir)
        decision_snapshot.BASE_DIR = base
        try:
            (base / "DEPLOYED_VERSION.json").write_text(
                json.dumps({"git_commit": "legacy-commit"}), encoding="utf-8"
            )
            assert decision_snapshot._git_commit() == "legacy-commit"
            (base / "DEPLOYED_GIT_COMMIT").write_text("marker-commit\n", encoding="utf-8")
            assert decision_snapshot._git_commit() == "marker-commit"
        finally:
            decision_snapshot.BASE_DIR = original


def test_sync_guard_contract() -> None:
    source = (ROOT / "_sync_to_server.py").read_text(encoding="utf-8")
    helpers = _literal_assignment(ROOT / "_sync_to_server.py", "HELPER_FILES")
    assert "_recheck_bugs.py" in helpers
    assert '"profitability_evidence.py"' in source
    assert '"origin/master"' in source
    assert "ETF_ALLOW_NON_MASTER_DEPLOY" in source
    assert "ETF_SYNC_PRICE_CSV" in source
    assert ".venv/bin/python -m py_compile" in source
    assert "DEPLOYED_GIT_COMMIT" in source
    assert "/.deploy/staging/" in source
    assert "/.deploy/backups/" in source
    assert "rollback_cmd" in source
    assert "curl -fsS" in source
    assert "HEALTH_OK" in source


def test_sync_guard_allows_local_data_only() -> None:
    status = " M auto_theme_signal.json\n M data/510300.csv\n M strategy.py\n"
    assert tracked_code_changes(status) == [" M strategy.py"]


def test_failed_remote_command_raises() -> None:
    class Channel:
        def __init__(self, code: int):
            self.code = code

        def recv_exit_status(self) -> int:
            return self.code

    class Stream:
        def __init__(self, data: bytes, code: int):
            self.data = data
            self.channel = Channel(code)

        def read(self) -> bytes:
            return self.data

    class SSH:
        def exec_command(self, _cmd: str, timeout: int = 120):
            return None, Stream(b"", 1), Stream(b"health failed", 1)

    try:
        run_checked(SSH(), "false")  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert "health failed" in str(exc)
    else:
        raise AssertionError("failed remote command was treated as success")


if __name__ == "__main__":
    test_cron_refresh_contract()
    test_snapshot_deployment_markers()
    test_sync_guard_contract()
    test_sync_guard_allows_local_data_only()
    test_failed_remote_command_raises()
    print("DEPLOYMENT CONSISTENCY OK")
