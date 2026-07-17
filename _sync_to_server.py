"""Sync application code from local repo to production server."""

from __future__ import annotations

import json
import os
import shlex
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
    "public_gateway.py",
    "strategy_review.py",
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
    "scripts/public_gateway.sh",
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
    "_test_daily_deadline.py",
    "_test_deployment_consistency.py",
    "_test_execution_consistency.py",
    "_test_profitability_evidence.py",
    "_test_live_personal.py",
    "_test_market_closed_frontend.py",
    "_test_personalization.py",
    "_test_profitability_controls.py",
    "_test_public_gateway.py",
    "_test_server_env.py",
    "_test_settlement_integrity.py",
    "_test_strategy_review.py",
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
    return result.stdout.rstrip()


def tracked_code_changes(status_output: str) -> list[str]:
    dirty: list[str] = []
    for line in status_output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].split(" -> ")[-1].replace("\\", "/")
        if not path.startswith("data/") and path != "auto_theme_signal.json":
            dirty.append(line)
    return dirty


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

        dirty = tracked_code_changes(
            _git("status", "--porcelain", "--untracked-files=no")
        )
        if dirty:
            raise SystemExit(
                "DEPLOY BLOCKED: tracked code/config has uncommitted changes:\n"
                + "\n".join(dirty[:20])
            )
        return head

    print("WARNING: ETF_ALLOW_NON_MASTER_DEPLOY=1 bypassed Git deployment checks")
    return _git("rev-parse", "HEAD")


def run_result(
    ssh: paramiko.SSHClient,
    cmd: str,
    timeout: int = 120,
) -> tuple[int, str]:
    _, out, err = ssh.exec_command(cmd, timeout=timeout)
    text = (out.read() + err.read()).decode("utf-8", errors="replace")
    return out.channel.recv_exit_status(), text


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    return run_result(ssh, cmd, timeout=timeout)[1]


def run_checked(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    code, output = run_result(ssh, cmd, timeout=timeout)
    if code != 0:
        raise RuntimeError(f"remote command failed ({code}): {cmd}\n{output}")
    return output


def sudo_checked(ssh: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    if not PASSWORD:
        raise SystemExit("ETF_SERVER_PASSWORD is missing. Add it to local .env first.")
    return run_checked(ssh, f"echo '{PASSWORD}' | sudo -S {cmd}", timeout=timeout)


def main() -> None:
    require_allowed_remote()
    if not PASSWORD:
        raise SystemExit("ETF_SERVER_PASSWORD is missing. Add it to local .env first.")
    commit = deployment_commit()
    allow_system_changes = os.environ.get("ETF_ALLOW_SYSTEM_CHANGES", "0") == "1"
    try:
        public_port = int(os.environ.get("ETF_PUBLIC_PORT", "3004"))
    except ValueError as exc:
        raise SystemExit("ETF_PUBLIC_PORT must be an integer") from exc
    if not 1024 <= public_port <= 65535:
        raise SystemExit("ETF_PUBLIC_PORT must be between 1024 and 65535")

    artifacts: list[tuple[str, Path]] = []
    for name in APP_FILES + HELPER_FILES:
        src = LOCAL / name
        if src.exists():
            artifacts.append((name, src))
        else:
            print("SKIP missing", name)

    if os.environ.get("ETF_SYNC_PRICE_CSV", "0") == "1":
        print("--- csv: intentional staged sync ---")
        try:
            from pool import ALL_POOL
        except Exception as exc:
            raise SystemExit(f"CSV sync requested but pool import failed: {exc}") from exc
        for item in ALL_POOL:
            code = str(item["code"]).zfill(6)
            src = LOCAL / "data" / f"{code}.csv"
            if not src.exists():
                raise SystemExit(f"CSV sync requested but file is missing: {src}")
            artifacts.append((f"data/{code}.csv", src))
    else:
        print("--- csv: skipped (set ETF_SYNC_PRICE_CSV=1 for an intentional data sync) ---")

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    staging = f"{REMOTE}/.deploy/staging/{stamp}-{commit[:12]}"
    backup = f"{REMOTE}/.deploy/backups/{stamp}-{commit[:12]}"

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=20)
    sftp = ssh.open_sftp()

    print(f"--- stage {len(artifacts)} files: {staging} ---")
    run_checked(ssh, f"mkdir -p {shlex.quote(staging)} {shlex.quote(backup)}")
    directories = sorted({name.rsplit("/", 1)[0] for name, _ in artifacts if "/" in name})
    if directories:
        run_checked(
            ssh,
            "mkdir -p " + " ".join(shlex.quote(f"{staging}/{item}") for item in directories),
        )
    for name, src in artifacts:
        sftp.put(str(src), f"{staging}/{name}")
        print("STAGE", name)
    with sftp.file(f"{staging}/MANIFEST", "w") as handle:
        handle.write("".join(f"{name}\n" for name, _ in artifacts))

    print("--- compile staged release ---")
    compile_targets = " ".join(
        shlex.quote(f"{staging}/{name}") for name, _ in artifacts if name.endswith(".py")
    )
    print(
        run_checked(
            ssh,
            f"{REMOTE}/.venv/bin/python -m py_compile {compile_targets} && echo COMPILE_OK",
            timeout=240,
        )
    )

    manifest = shlex.quote(f"{staging}/MANIFEST")
    backup_q = shlex.quote(backup)
    remote_q = shlex.quote(REMOTE)
    backup_cmd = (
        f"set -e; while IFS= read -r f; do "
        f"if [ -e {remote_q}/\"$f\" ]; then mkdir -p {backup_q}/\"$(dirname \"$f\")\"; "
        f"cp -a {remote_q}/\"$f\" {backup_q}/\"$f\"; "
        f"else printf '%s\\n' \"$f\" >> {backup_q}/.missing; fi; "
        f"done < {manifest}; "
        f"for f in DEPLOYED_GIT_COMMIT DEPLOYED_VERSION.json; do "
        f"if [ -e {remote_q}/\"$f\" ]; then cp -a {remote_q}/\"$f\" {backup_q}/\"$f\"; fi; done"
    )
    promote_cmd = (
        f"set -e; while IFS= read -r f; do mkdir -p {remote_q}/\"$(dirname \"$f\")\"; "
        f"cp -a {shlex.quote(staging)}/\"$f\" {remote_q}/\"$f.deploy-new\"; "
        f"mv -f {remote_q}/\"$f.deploy-new\" {remote_q}/\"$f\"; "
        f"done < {manifest}"
    )
    rollback_cmd = (
        f"set -e; while IFS= read -r f; do "
        f"if [ -e {backup_q}/\"$f\" ]; then mkdir -p {remote_q}/\"$(dirname \"$f\")\"; "
        f"cp -a {backup_q}/\"$f\" {remote_q}/\"$f.rollback\"; "
        f"mv -f {remote_q}/\"$f.rollback\" {remote_q}/\"$f\"; "
        f"else rm -f {remote_q}/\"$f\"; fi; done < {manifest}; "
        f"for f in DEPLOYED_GIT_COMMIT DEPLOYED_VERSION.json; do "
        f"if [ -e {backup_q}/\"$f\" ]; then cp -a {backup_q}/\"$f\" {remote_q}/\"$f.rollback\"; "
        f"mv -f {remote_q}/\"$f.rollback\" {remote_q}/\"$f\"; fi; done"
    )

    promoted = False
    try:
        print(f"--- backup current release: {backup} ---")
        run_checked(ssh, backup_cmd, timeout=240)
        print("--- promote staged release ---")
        promoted = True
        run_checked(ssh, promote_cmd, timeout=240)

        print("--- generate current-code review (official output remains unchanged) ---")
        review_date_cmd = "$(TZ=Asia/Shanghai date +%F)"
        print(
            run_checked(
                ssh,
                f"cd {remote_q} && env ETF_GIT_COMMIT={shlex.quote(commit)} "
                f".venv/bin/python strategy_review.py --date {review_date_cmd}",
                timeout=240,
            )
        )

        if allow_system_changes:
            print("--- administrator-authorized systemd integration ---")
            print(sudo_checked(ssh, f"cp {REMOTE}/etf-dashboard.service /etc/systemd/system/etf-dashboard.service"))
            print(sudo_checked(ssh, f"cp {REMOTE}/etf-agent-chat.service /etc/systemd/system/etf-agent-chat.service"))
            print(sudo_checked(ssh, "systemctl daemon-reload"))
            print(sudo_checked(ssh, "systemctl restart etf-dashboard etf-agent-chat"))
            time.sleep(2)
        else:
            print("--- folder-only runtime (no sudo/systemd/nginx changes) ---")
            run_checked(
                ssh,
                f"chmod +x {REMOTE}/scripts/public_gateway.sh "
                f"{REMOTE}/scripts/post_close_sync.sh",
            )
            print(
                run_checked(
                    ssh,
                    f"cd {REMOTE} && env ETF_PUBLIC_PORT={public_port} "
                    "scripts/public_gateway.sh restart",
                )
            )

        print("--- strict health ---")
        if allow_system_changes:
            health_command = (
                "systemctl is-active --quiet etf-dashboard etf-agent-chat "
                "&& curl -fsS http://127.0.0.1/etf-agent/ >/dev/null "
                "&& curl -fsS http://127.0.0.1/etf-agent/chat/ >/dev/null "
                "&& curl -fsS http://127.0.0.1/etf-agent/chat/docs >/dev/null "
                "&& echo HEALTH_OK"
            )
        else:
            health_command = (
                f"curl -fsS http://127.0.0.1:{public_port}/healthz >/dev/null "
                f"&& curl -fsS http://127.0.0.1:{public_port}/etf-agent/ >/dev/null "
                f"&& curl -fsS http://127.0.0.1:{public_port}/etf-agent/chat/ >/dev/null "
                f"&& curl -fsS http://127.0.0.1:{public_port}/etf-agent/chat/docs >/dev/null "
                "&& echo HEALTH_OK"
            )
        health = run_checked(ssh, health_command)
        if "HEALTH_OK" not in health:
            raise RuntimeError("strict health check did not confirm success")
        print(health)

        metadata = {
            "commit": commit,
            "git_commit": commit,
            "deployed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": "origin/master",
            "backup": backup,
            "system_changes_authorized": allow_system_changes,
            "deployment_mode": "systemd" if allow_system_changes else "folder_only",
        }
        with sftp.file(f"{staging}/DEPLOYED_GIT_COMMIT", "w") as handle:
            handle.write(commit + "\n")
        with sftp.file(f"{staging}/DEPLOYED_VERSION.json", "w") as handle:
            handle.write(json.dumps(metadata, ensure_ascii=True, indent=2) + "\n")
        run_checked(
            ssh,
            f"for f in DEPLOYED_GIT_COMMIT DEPLOYED_VERSION.json; do "
            f"cp -a {shlex.quote(staging)}/\"$f\" {remote_q}/\"$f.deploy-new\"; "
            f"mv -f {remote_q}/\"$f.deploy-new\" {remote_q}/\"$f\"; done",
        )
    except Exception as exc:
        if promoted:
            print(f"DEPLOY FAILED, rolling back from {backup}: {exc}")
            run_checked(ssh, rollback_cmd, timeout=240)
            if allow_system_changes:
                print(sudo_checked(ssh, f"cp {REMOTE}/etf-dashboard.service /etc/systemd/system/etf-dashboard.service"))
                print(sudo_checked(ssh, f"cp {REMOTE}/etf-agent-chat.service /etc/systemd/system/etf-agent-chat.service"))
                print(sudo_checked(ssh, "systemctl daemon-reload"))
                print(sudo_checked(ssh, "systemctl restart etf-dashboard etf-agent-chat"))
            else:
                print(
                    run_checked(
                        ssh,
                        f"cd {REMOTE} && env ETF_PUBLIC_PORT={public_port} "
                        "scripts/public_gateway.sh restart",
                    )
                )
        sftp.close()
        ssh.close()
        raise SystemExit(f"DEPLOY FAILED: {exc}") from exc

    sftp.close()
    ssh.close()
    print(f"SYNC DONE ({len(artifacts)} files, backup={backup})")


if __name__ == "__main__":
    main()
