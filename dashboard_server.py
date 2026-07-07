"""Local dashboard for the ETF daily agent.

Run ``start_dashboard.bat`` and open http://127.0.0.1:8765.
The dashboard is read-only by default, with an optional button that calls the
existing ``daily_job.py`` to generate today's prediction.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import daily_pnl
from daily_pnl import _load_bar
import llm_client
import llm_decider

__file__ = Path(__file__).resolve()
BASE_DIR = __file__.parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
NEWS_DIR = BASE_DIR / "data" / "daily_news_signal"
HOST = "127.0.0.1"
PORT = 8765

INDEX_HTML_PATH = BASE_DIR / "dashboard.html"
TEAM_CONFIG_PATH = BASE_DIR / "data" / "team_config.json"
TEAM_CONFIG_EXAMPLE_PATH = BASE_DIR / "data" / "team_config.example.json"


def _load_html() -> str:
    return INDEX_HTML_PATH.read_text(encoding="utf-8")


def _load_team_config() -> dict[str, Any]:
    """读取参赛团队信息（团队名/口令/提交截止时间等），仅用于本地仪表盘展示。

    ``team_config.json`` 已加入 .gitignore，不会随代码提交；未配置时回退到
    示例文件并标记 configured=False，前端据此提示用户先填写。
    """
    path = TEAM_CONFIG_PATH if TEAM_CONFIG_PATH.exists() else TEAM_CONFIG_EXAMPLE_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    placeholder_markers = ("填写", "示例")
    configured = TEAM_CONFIG_PATH.exists() and not any(
        isinstance(v, str) and any(m in v for m in placeholder_markers)
        for k, v in data.items()
        if k in ("team_name", "leader_name", "leader_phone", "submit_password")
    )
    data["configured"] = configured
    return data

def _read_json(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_file(pattern: str, directory: Path, only_weekdays: bool = False) -> Path | None:
    today = datetime.now().date()
    hits = []
    for p in sorted(directory.glob(pattern), reverse=True):
        try:
            d = datetime.strptime(p.name.split("_")[0], "%Y-%m-%d").date()
        except ValueError:
            continue
        if only_weekdays and d.weekday() >= 5:
            continue
        hits.append((d, p))
        if d <= today:
            break
    if hits:
        hits.sort(key=lambda x: x[0], reverse=True)
        return hits[0][1]
    return None


def _settle_prediction(full_path: Path) -> dict[str, Any] | None:
    if not full_path.exists():
        return None
    date_str = full_path.name.split("_")[0]
    try:
        data = json.loads(full_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    comp = data.get("competition_output", [])
    if not comp:
        return None
    pnl_rows = []
    total_pnl = 0.0
    for item in comp:
        code = item.get("symbol", "")
        name = item.get("symbol_name", "")
        volume = item.get("volume", 0)
        # fetch the settle bar
        bar = _load_bar(code, date_str)
        if bar is None:
            continue
        prev_close = float(bar.get("open", 0))
        close = float(bar.get("close", 0))
        if prev_close == 0:
            continue
        pnl = round(volume * (close / prev_close - 1) - volume * 6 / 10000, 2)
        total_pnl += pnl
        pnl_rows.append(dict(code=code, name=name, volume=volume, open=prev_close, close=close, pnl=pnl))
    return dict(prediction_date=date_str, total_pnl=round(total_pnl, 2), rows=pnl_rows)


def _system_info() -> dict[str, Any]:
    today = datetime.now()
    today_str = today.strftime("%Y-%m-%d")
    wd = today.weekday()
    wd_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return {
        "strategy_label": "",
        "prompt_file": llm_decider.PROMPT_PATH.name,
        "llm_available": llm_client.is_available(),
        "today_is_weekend": wd >= 5,
        "weekday_name": wd_names[wd],
        "today": today_str,
    }


def load_status(view_date: str | None = None) -> dict[str, Any]:
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    if view_date:
        try:
            target = datetime.strptime(view_date, "%Y-%m-%d").date()
        except ValueError:
            target = today
    else:
        target = today

    # find full json
    full_path = _latest_file(f"{target.strftime('%Y-%m-%d')}*_full.json", OUTPUT_DIR)
    submit_path = _latest_file(f"{target.strftime('%Y-%m-%d')}*_submit.json", OUTPUT_DIR)
    news_path = _latest_file(f"{target.strftime('%Y-%m-%d')}.json", NEWS_DIR)

    full = _read_json(full_path)
    submit = _read_json(submit_path)
    news = _read_json(news_path)

    previous_pnl = _settle_prediction(full_path) if full_path else None

    return {
        "today": today_str,
        "view_date": str(target),
        "today_predicted": full_path is not None and full_path.name.startswith(today_str),
        "today_is_weekend": target.weekday() >= 5 if view_date else today.weekday() >= 5,
        "latest_date": full_path.name.split("_")[0] if full_path else None,
        "full": full,
        "submit": submit,
        "news_signal": news,
        "previous_pnl": previous_pnl,
        "full_path": str(full_path) if full_path else None,
        "submit_path": str(submit_path) if submit_path else None,
        "news_path": str(news_path) if news_path else None,
        "system_info": _system_info(),
        "team_config": _load_team_config(),
    }


def _json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _run_daily_job(options: dict[str, Any]) -> dict[str, Any]:
    cmd = [sys.executable, str(BASE_DIR / "daily_job.py")]
    today = datetime.now().date()
    if today.weekday() < 5:
        cmd.append("--date")
        cmd.append(today.strftime("%Y-%m-%d"))
    if options.get("skip_price_update"):
        cmd.append("--skip-price-update")
    force = bool(options.get("force"))
    if force:
        cmd.append("--force")

    try:
        from etf_agent_chat import _backup_today_outputs
        _backup_today_outputs()
    except Exception:
        pass

    import daily_run_guard
    today_str = today.strftime("%Y-%m-%d")
    if not force and today.weekday() < 5 and daily_run_guard.has_daily_run(today_str):
        return {"output": "今日预测已存在，跳过重复运行（请用「强制重跑」覆盖）。", "status": "skipped"}

    load_submit = options.get("load_submit")
    if load_submit:
        cmd.append("--load-submit")
        if isinstance(load_submit, str):
            cmd.append(load_submit)

    env = dict(os.environ)
    env.setdefault("ETF_AGENT_STRICT_DATA", "1")

    try:
        r = subprocess.run(cmd, cwd=str(BASE_DIR), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=360)
        output = r.stdout + r.stderr
        return {"output": output[-3000:], "status": "ok" if r.returncode == 0 else "error", "returncode": r.returncode}
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or b"").decode("utf-8", errors="replace") + (e.stderr or b"").decode("utf-8", errors="replace")
        return {"output": output[-3000:] + "\n[超时]", "status": "timeout"}
    except Exception as e:
        return {"output": str(e), "status": "error"}


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            html = _load_html()
            self.send_header("Content-Length", str(len(html.encode("utf-8"))))
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
        elif parsed.path == "/api/status":
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            view_date = qs.get("date", [None])[0]
            self._json_response(load_status(view_date))
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            result = _run_daily_job(body)
            self._json_response(result)
        else:
            self.send_error(404)

    def _json_response(self, payload, status=200):
        _json_response(self, payload, status)

    def log_message(self, format, *args):
        pass  # suppress default logging


def main():
    parser = argparse.ArgumentParser(description="ETF Agent Dashboard")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="不自动打开浏览器（配合 start_auto.bat 由外层脚本延时打开截图模式页面，避免开两个标签页）。",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Dashboard running at {url}")

    if not args.no_browser:
        def _open_browser() -> None:
            time.sleep(0.4)
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
