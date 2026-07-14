"""Sync application code from local repo to production server."""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import paramiko

from server_env import HOST, PASSWORD, REMOTE, USER, require_allowed_remote

LOCAL = Path(__file__).resolve().parent

APP_FILES = [
    # Core decision pipeline
    "pool.py",
    "indicators.py",
    "features.py",
    "backtest_provenance.py",
    "profitability_evidence.py",
    "scoring.py",
    "position.py",
    "strategy.py",
    "decision_integrity.py",
    "stability_risk.py",
    "goal_state.py",
    "decision_snapshot.py",
    "trading_calendar.py",
    "daily_job.py",
    "daily_pnl.py",
    "daily_run_guard.py",
    "settlement_prices.py",
    "market_data.py",
    "update_local_csv.py",
    "post_close_sync.py",
    # News / econ / LLM
    "theme_signal.py",
    "news_signal.py",
    "news_fetcher.py",
    "news_llm_scorer.py",
    "news_time_split.py",
    "news_store.py",
    "econ_calendar.py",
    "llm_client.py",
    "llm_decider.py",
    "prompts/decider_zh.md",
    # Agent / dashboard / isolation
    "agent_server.py",
    "agent_orchestrator.py",
    "agent_kb.py",
    "competition_guard.py",
    "dashboard_server.py",
    "dashboard.html",
    "docs.html",
    "etf_agent_chat.py",
    "info_collector.py",
    "live_personal_runner.py",
    "personalized_advisor.py",
    "security_guard.py",
    "session_store.py",
    "server_env.py",
    # Deploy units
    "etf-dashboard.service",
    "etf-agent-chat.service",
    "nginx_etf_agent_chat.conf",
    "nginx_etf_agent_security.conf",
    "scripts/post_close_sync.sh",
    "start_agent.bat",
]

HELPER_FILES = [
    "_audit_server_security.py",
    "_backtest_current.py",
    "_backtest_full_pipeline.py",
    "_check_pnl.py",
    "_check_pnl2.py",
    "_check_public.py",
    "_check_recent_preds.py",
    "_check_same_etf_pnl.py",
    "_check_sync_status.py",
    "_deploy_chat.py",
    "_deploy_isolation.py",
    "_deploy_live_personal.py",
    "_deploy_personalization.py",
    "_deploy_post_close.py",
    "_deploy_security.py",
    "_diagnose_same_stock.py",
    "_force_rerun_dates.py",
    "_force_rerun_today.py",
    "_fix_api_base.py",
    "_fix_chat_service.py",
    "_fix_nginx_rate.py",
    "_pull_full_json.py",
    "_recheck_bugs.py",
    "_restart_dashboard.py",
    "_restore_systemd.py",
    "_run_post_close.py",
    "_start_dashboard.py",
    "_sync_to_server.py",
    "_test_agent_chat.py",
    "_test_akshare.py",
    "_test_competition_isolation.py",
    "_test_decision_integrity.py",
    "_test_deployment_consistency.py",
    "_test_execution_consistency.py",
    "_test_profitability_evidence.py",
    "_test_live_personal.py",
    "_test_market_closed_frontend.py",
    "_test_personalization.py",
    "_test_profitability_controls.py",
    "_test_settlement_integrity.py",
    "_evaluate_profitability.py",
    "_test_sources.py",
    "_test_sources2.py",
]


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=LOCAL,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def deployment_commit() -> str:
    """Refuse accidental production deploys from stale or edited code."""
    required = LOCAL / "profitability_evidence.py"
    if not required.exists():
        raise SystemExit("DEPLOY BLOCKED: profitability_evidence.py is missing")

    allow_unsafe = os.environ.get("ETF_ALLOW_NON_MASTER_DEPLOY", "0") == "1"
    if not allow_unsafe:
        _git("fetch", "origin", "master", "--quiet")
        head = _git("rev-parse", "HEAD")
        origin_master = _git("rev-parse", "origin/master")
        if head != origin_master:
            raise SystemExit(
                "DEPLOY BLOCKED: local HEAD is not origin/master "
                f"(HEAD={head[:12]}, origin/master={origin_master[:12]}). "
                "Merge/pull the published version first."
            )

        dirty = []
        for line in _git("status", "--porcelain", "--untracked-files=no").splitlines():
            path = line[3:].split(" -> ")[-1].replace("\\", "/")
            if not path.startswith("data/") and path != "auto_theme_signal.json":
                dirty.append(line)
        if dirty:
            raise SystemExit(
                "DEPLOY BLOCKED: tracked code/config has uncommitted changes:\n"
                + "\n".join(dirty[:20])
            )
        return head

    print("WARNING: ETF_ALLOW_NON_MASTER_DEPLOY=1 bypassed Git deployment checks")
    return _git("rev-parse", "HEAD")


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    return (out.read() + err.read()).decode("utf-8", errors="replace")


def sudo(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    if not PASSWORD:
        raise SystemExit("ETF_SERVER_PASSWORD is missing. Add it to local .env first.")
    return run(ssh, f"echo '{PASSWORD}' | sudo -S {cmd}", timeout=timeout)


def main() -> None:
    require_allowed_remote()
    if not PASSWORD:
        raise SystemExit("ETF_SERVER_PASSWORD is missing. Add it to local .env first.")
    commit = deployment_commit()

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    uploaded: list[str] = []
    for name in APP_FILES + HELPER_FILES:
        src = LOCAL / name
        if not src.exists():
            print("SKIP missing", name)
            continue
        dst = f"{REMOTE}/{name}"
        if name.startswith("scripts/") or name.startswith("prompts/"):
            parent = name.rsplit("/", 1)[0]
            run(ssh, f"mkdir -p {REMOTE}/{parent}")
        sftp.put(str(src), dst)
        uploaded.append(name)
        print("UP", name)

    if os.environ.get("ETF_SYNC_PRICE_CSV", "0") == "1":
        print("--- csv ---")
        try:
            from pool import ALL_POOL
        except Exception as exc:
            print("SKIP csv import", exc)
            ALL_POOL = []
        run(ssh, f"mkdir -p {REMOTE}/data")
        csv_n = 0
        for item in ALL_POOL:
            code = str(item["code"]).zfill(6)
            src = LOCAL / "data" / f"{code}.csv"
            if not src.exists():
                print("SKIP missing csv", code)
                continue
            sftp.put(str(src), f"{REMOTE}/data/{code}.csv")
            csv_n += 1
            print("UP csv", code)
        print(f"CSV synced: {csv_n}")
    else:
        print("--- csv: skipped (set ETF_SYNC_PRICE_CSV=1 for an intentional data sync) ---")

    print("--- compile ---")
    compile_targets = " ".join(
        p for p in (
            "pool.py",
            "security_guard.py",
            "dashboard_server.py",
            "agent_server.py",
            "agent_orchestrator.py",
            "competition_guard.py",
            "daily_job.py",
            "decision_integrity.py",
            "strategy.py",
            "scoring.py",
            "position.py",
            "update_local_csv.py",
            "post_close_sync.py",
            "llm_decider.py",
            "llm_client.py",
            "etf_agent_chat.py",
            "live_personal_runner.py",
            "personalized_advisor.py",
            "info_collector.py",
            "session_store.py",
        )
        if (LOCAL / p).exists()
    )
    compile_output = run(
        ssh,
        f"cd {REMOTE} && .venv/bin/python -m py_compile {compile_targets} && echo COMPILE_OK",
    )
    print(compile_output)
    if "COMPILE_OK" not in compile_output:
        sftp.close()
        ssh.close()
        raise SystemExit("DEPLOY FAILED: remote compile did not complete")

    metadata = {
        "commit": commit,
        "git_commit": commit,
        "deployed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "origin/master",
    }
    with sftp.file(f"{REMOTE}/DEPLOYED_GIT_COMMIT", "w") as handle:
        handle.write(commit + "\n")
    with sftp.file(f"{REMOTE}/DEPLOYED_VERSION.json", "w") as handle:
        handle.write(json.dumps(metadata, ensure_ascii=True, indent=2) + "\n")

    print("--- systemd ---")
    print(sudo(ssh, f"cp {REMOTE}/etf-dashboard.service /etc/systemd/system/etf-dashboard.service"))
    print(sudo(ssh, f"cp {REMOTE}/etf-agent-chat.service /etc/systemd/system/etf-agent-chat.service"))
    print(sudo(ssh, "systemctl daemon-reload"))
    print(sudo(ssh, "systemctl restart etf-dashboard etf-agent-chat"))
    time.sleep(2)
    print(run(ssh, "systemctl is-active etf-dashboard etf-agent-chat"))
    print(run(ssh, "ss -lntp | grep -E ':8765|:8766' || true"))

    print("--- health ---")
    print(run(ssh, "curl -s -o /dev/null -w 'dash=%{http_code}\\n' http://127.0.0.1/etf-agent/"))
    print(run(ssh, "curl -s -o /dev/null -w 'chat=%{http_code}\\n' http://127.0.0.1/etf-agent/chat/"))

    sftp.close()
    ssh.close()
    print(f"SYNC DONE ({len(uploaded)} files)")


if __name__ == "__main__":
    main()
