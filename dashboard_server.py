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
import security_guard

__file__ = Path(__file__).resolve()
BASE_DIR = __file__.parent
OUTPUT_DIR = BASE_DIR / "data" / "daily_output"
NEWS_DIR = BASE_DIR / "data" / "daily_news_signal"
HOST = "127.0.0.1"
PORT = 8765

INDEX_HTML_PATH = BASE_DIR / "dashboard.html"
TEAM_CONFIG_PATH = BASE_DIR / "data" / "team_config.json"
TEAM_CONFIG_EXAMPLE_PATH = BASE_DIR / "data" / "team_config.example.json"
_RUN_LOCK = threading.Lock()


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
    """按平台结算口径复盘预测日盈亏：买入价=昨收，卖出价=今收。

    查看日若为「今天」且尚未收盘（15:00 前），返回 pending，避免半截K误报。
    """
    if not full_path.exists():
        return None
    date_str = full_path.name.split("_")[0]
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if date_str == today_str and now.hour < 15:
        try:
            data = json.loads(full_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        comp = data.get("competition_output", []) if isinstance(data, dict) else []
        return dict(
            prediction_date=date_str,
            total_pnl=0.0,
            rows=[],
            pending=True,
            holdings_count=len(comp),
            settled_count=0,
            reason="market_not_closed",
        )
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
        bar = _load_bar(code, date_str)
        if bar is None:
            continue
        prev_close = float(bar.get("prev_close", 0))
        close = float(bar.get("close", 0))
        if prev_close == 0:
            continue
        # 平台口径：amount=volume×昨收，pnl=amount×(今收-昨收)/昨收，
        # 化简即 volume×(今收-昨收)。
        pnl = round(volume * (close - prev_close), 2)
        total_pnl += pnl
        pnl_rows.append(dict(
            code=code, name=name, volume=volume,
            prev_close=prev_close, open=prev_close, close=close, pnl=pnl,
        ))
    pending = bool(comp) and len(pnl_rows) < len(comp)
    return dict(
        prediction_date=date_str,
        total_pnl=round(total_pnl, 2),
        rows=pnl_rows,
        pending=pending,
        holdings_count=len(comp),
        settled_count=len(pnl_rows),
    )


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

    # Default view falls back to the latest completed trading-day output. An
    # explicitly selected date remains exact so historical inspection is clear.
    full_path = _latest_file(f"{target.strftime('%Y-%m-%d')}*_full.json", OUTPUT_DIR)
    if full_path is None and view_date is None:
        full_path = _latest_file("*_full.json", OUTPUT_DIR, only_weekdays=True)

    output_date = full_path.name.split("_")[0] if full_path else target.strftime("%Y-%m-%d")
    submit_path = _latest_file(f"{output_date}*_submit.json", OUTPUT_DIR)
    news_path = _latest_file(f"{output_date}.json", NEWS_DIR)

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


def _build_daily_job_cmd(options: dict[str, Any]) -> tuple[list[str], dict[str, str], str]:
    """组装 daily_job 命令行；返回 (cmd, env, today_str)。"""
    cmd = [sys.executable, "-u", str(BASE_DIR / "daily_job.py")]
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    from trading_calendar import is_trading_day

    if is_trading_day(today):
        cmd.extend(["--date", today_str])
    if options.get("skip_price_update"):
        cmd.append("--skip-price-update")
    if options.get("force"):
        cmd.append("--force")

    # --load-submit 已从 daily_job 移除，忽略前端遗留字段

    env = dict(os.environ)
    env.setdefault("ETF_AGENT_STRICT_DATA", "1")
    env.setdefault("SCORE_GATE_MODE", "static")
    env["CAPITAL"] = "500000"
    env["PYTHONUNBUFFERED"] = "1"
    return cmd, env, today_str


def _prepare_daily_job(options: dict[str, Any]) -> tuple[list[str], dict[str, str]] | dict[str, Any]:
    """若可运行则返回 (cmd, env)；否则返回 skipped/busy 状态 dict。"""
    if not _RUN_LOCK.acquire(blocking=False):
        return {"output": "已有预测任务在运行，请等待当前任务结束。", "status": "busy"}

    try:
        force = bool(options.get("force"))
        if force:
            try:
                from etf_agent_chat import _backup_today_outputs
                _backup_today_outputs()
            except Exception:
                pass

        import daily_run_guard
        from trading_calendar import is_trading_day

        today_str = datetime.now().strftime("%Y-%m-%d")
        if not force and is_trading_day(today_str) and daily_run_guard.has_daily_run(today_str):
            _RUN_LOCK.release()
            return {
                "output": "今日预测已存在，跳过重复运行（请用「强制重跑」覆盖）。",
                "status": "skipped",
            }

        cmd, env, _ = _build_daily_job_cmd(options)
        return cmd, env
    except Exception:
        _RUN_LOCK.release()
        raise


def _run_daily_job(options: dict[str, Any]) -> dict[str, Any]:
    prepared = _prepare_daily_job(options)
    if isinstance(prepared, dict):
        return prepared
    cmd, env = prepared
    try:
        r = subprocess.run(
            cmd,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=360,
            env=env,
        )
        output = r.stdout + r.stderr
        return {
            "output": output[-3000:],
            "status": "ok" if r.returncode == 0 else "error",
            "returncode": r.returncode,
        }
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or b"").decode("utf-8", errors="replace") + (e.stderr or b"").decode("utf-8", errors="replace")
        return {"output": output[-3000:] + "\n[超时]", "status": "timeout"}
    except Exception as e:
        return {"output": str(e), "status": "error"}
    finally:
        if _RUN_LOCK.locked():
            _RUN_LOCK.release()


def _write_ndjson_line(handler: BaseHTTPRequestHandler, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False) + "\n"
    handler.wfile.write(line.encode("utf-8"))
    handler.wfile.flush()


def _stream_daily_job(handler: BaseHTTPRequestHandler, options: dict[str, Any]) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()

    prepared = _prepare_daily_job(options)
    if isinstance(prepared, dict):
        msg = prepared.get("output", "")
        if msg:
            _write_ndjson_line(handler, {"type": "log", "text": msg})
        _write_ndjson_line(handler, {"type": "done", **prepared})
        return

    cmd, env = prepared
    output_lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        assert proc.stdout is not None
        start = time.time()
        while True:
            line = proc.stdout.readline()
            if line:
                text = line.rstrip("\r\n")
                output_lines.append(text)
                _write_ndjson_line(handler, {"type": "log", "text": text})
                continue
            if proc.poll() is not None:
                break
            if time.time() - start > 360:
                proc.kill()
                _write_ndjson_line(handler, {"type": "log", "text": "[超时] 预测运行超过 6 分钟，已终止。"})
                _write_ndjson_line(handler, {"type": "done", "status": "timeout", "output": "\n".join(output_lines[-3000:])})
                return
            time.sleep(0.05)

        tail = "\n".join(output_lines)
        status = "ok" if proc.returncode == 0 else "error"
        _write_ndjson_line(
            handler,
            {
                "type": "done",
                "status": status,
                "returncode": proc.returncode,
                "output": tail[-3000:],
            },
        )
    except Exception as exc:
        _write_ndjson_line(handler, {"type": "log", "text": f"[错误] {exc}"})
        _write_ndjson_line(handler, {"type": "done", "status": "error", "output": str(exc)})
    finally:
        if _RUN_LOCK.locked():
            _RUN_LOCK.release()


def _today_advice(view_date: str | None) -> list[dict[str, Any]]:
    """当日投资建议，纯 JSON 数组格式，供比赛平台自动拉取。

    对应 investment-daily-submit.html 描述的机器解析格式：
    ``[{"symbol", "symbol_name", "volume"}, ...]``；无预测或空仓时返回
    ``[]``（而非错误对象），避免打断调度程序的 JSON 解析。

    注：本项目暂未拿到平台「自动调用与结算对接」的技术对接文档，此接口
    是按公开的 JSON 格式规范预先搭好的；具体调用路径/鉴权方式请以正式
    对接文档为准，届时可能需要调整路由或加签名校验。
    """
    status = load_status(view_date)
    full = status.get("full") or {}
    comp = full.get("competition_output")
    return comp if isinstance(comp, list) else []


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
        elif parsed.path in ("/api/today_advice", "/api/submit"):
            from urllib.parse import parse_qs
            qs = parse_qs(parsed.query)
            view_date = qs.get("date", [None])[0]
            payload = _today_advice(view_date)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            blocked = security_guard.check_api_run(self, body)
            if blocked:
                status_code = 429 if blocked.get("status") == "rate_limited" else 403
                if body.get("stream", True):
                    handler = self
                    handler.send_response(status_code)
                    handler.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    handler.end_headers()
                    msg = blocked.get("output", "请求被拒绝")
                    _write_ndjson_line(handler, {"type": "log", "text": msg})
                    _write_ndjson_line(handler, {"type": "done", **blocked})
                else:
                    self._json_response(blocked, status=status_code)
                return
            if body.get("stream", True):
                _stream_daily_job(self, body)
            else:
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
