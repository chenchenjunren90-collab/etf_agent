"""ETF investment agent — chat UI + API (port 8766)."""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_kb import load_knowledge_base, rebuild_knowledge_base
from etf_agent_chat import handle_message

HOST = "127.0.0.1"
PORT = 8766

CHAT_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>ETF 投资智能体</title>
  <style>
    :root, html[data-theme="dark"] {
      --bg:#0b1220; --panel:#111c33; --line:#26344f;
      --text:#e5e7eb; --muted:#94a3b8; --accent:#38bdf8; --user:#1d4ed8;
      --chip-bg:#0b1222; --input-bg:#0b1222; --aside-bg:rgba(17,28,51,.6);
      --btn-fg:#001018;
    }
    html[data-theme="light"] {
      --bg:#f1f5f9; --panel:#ffffff; --line:#cbd5e1;
      --text:#0f172a; --muted:#64748b; --accent:#0284c7; --user:#2563eb;
      --chip-bg:#f8fafc; --input-bg:#ffffff; --aside-bg:rgba(248,250,252,.95);
      --btn-fg:#f8fafc;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:"Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;transition:background .2s,color .2s}
    header{position:relative;z-index:20;padding:16px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;background:var(--bg)}
    h1{margin:0;font-size:20px}
    .sub{color:var(--muted);font-size:13px}
    .header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
    .theme-btn{border:1px solid var(--line);border-radius:10px;padding:8px 14px;background:var(--panel);color:var(--text);font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap}
    .theme-btn:hover{border-color:var(--accent);color:var(--accent)}
    main{flex:1;display:grid;grid-template-columns:280px 1fr;min-height:0}
    aside{border-right:1px solid var(--line);padding:14px;overflow:auto;background:var(--aside-bg)}
    aside h3{margin:0 0 10px;font-size:14px;color:var(--muted)}
    .chip{display:block;padding:8px 10px;margin-bottom:8px;border:1px solid var(--line);border-radius:10px;background:var(--chip-bg);cursor:pointer;font-size:13px;color:var(--text)}
    .chip:hover{border-color:var(--accent)}
    #chat{display:flex;flex-direction:column;min-height:0;background:var(--bg)}
    #messages{flex:1;overflow:auto;padding:18px 20px;display:flex;flex-direction:column;gap:12px}
    .msg{max-width:85%;padding:12px 14px;border-radius:14px;line-height:1.55;font-size:14px;white-space:pre-wrap}
    .bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);color:var(--text)}
    .user{align-self:flex-end;background:var(--user);color:#f8fafc}
    .meta{font-size:11px;color:var(--muted);margin-top:4px}
    #composer{border-top:1px solid var(--line);padding:12px 16px;display:flex;gap:10px;background:var(--bg)}
    textarea{flex:1;min-height:52px;max-height:120px;resize:vertical;border:1px solid var(--line);border-radius:12px;background:var(--input-bg);color:var(--text);padding:10px 12px;font-size:14px}
  #send{border:0;border-radius:10px;padding:10px 16px;background:var(--accent);color:var(--btn-fg);font-weight:600;cursor:pointer}
    pre{background:var(--input-bg);border:1px solid var(--line);padding:10px;border-radius:8px;overflow:auto;font-size:12px;color:var(--text)}
    a{color:var(--accent)}
    @media(max-width:800px){main{grid-template-columns:1fr}aside{display:none}}
  </style>
</head>
<body data-theme="light">
  <header>
    <div>
      <h1>ETF 投资智能体</h1>
      <div class="sub">比赛提交 · 新闻知识库 · 持仓解读</div>
    </div>
    <div class="header-right">
      <div class="sub" id="kbInfo">知识库加载中…</div>
      <button type="button" class="theme-btn" id="themeToggle" title="切换白天/黑夜模式">黑夜模式</button>
    </div>
  </header>
  <main>
    <aside>
      <h3>快捷提问</h3>
      <div class="chip" data-q="今日比赛预测指令">今日比赛预测指令</div>
      <div class="chip" data-q="测一下今天">测一下今天预测</div>
      <div class="chip" data-q="为什么选这些ETF">为什么选这些 ETF</div>
      <div class="chip" data-q="今日筛选后的新闻有哪些">今日筛选后的新闻</div>
      <div class="chip" data-q="今天三只持仓的收盘价是多少">今日持仓收盘价</div>
      <div class="chip" data-q="今天预测的收益是多少">今日盘后收益</div>
      <div class="chip" data-q="昨天赚了多少钱">昨天收益复盘</div>
      <div class="chip" data-q="这条新闻对证券ETF有利吗：券商板块成交放量">新闻是否利好证券 ETF</div>
      <h3 style="margin-top:16px">定位说明</h3>
      <p class="sub" style="margin:0;line-height:1.5">仅回答 ETF 投资、比赛持仓、已筛选新闻与持仓原因；投资无关问题将引导回正题。</p>
    </aside>
    <section id="chat">
      <div id="messages"></div>
      <div id="composer">
        <textarea id="input" placeholder="例如：今日比赛预测 / 为什么买 518880 / 某新闻对黄金ETF是否有利"></textarea>
        <button id="send">发送</button>
      </div>
    </section>
  </main>
  <script>
    const messages = document.getElementById('messages');
    const input = document.getElementById('input');
    const kbInfo = document.getElementById('kbInfo');
    const themeToggle = document.getElementById('themeToggle');
    const THEME_KEY = 'etf-agent-theme';

    function storageGet(key) {
      try { return localStorage.getItem(key); } catch (e) { return null; }
    }
    function storageSet(key, val) {
      try { localStorage.setItem(key, val); } catch (e) {}
    }

    function applyTheme(theme) {
      const t = theme === 'light' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', t);
      document.body.setAttribute('data-theme', t);
      if (themeToggle) {
        themeToggle.textContent = t === 'dark' ? '白天模式' : '黑夜模式';
        themeToggle.title = t === 'dark' ? '切换到浅色界面' : '切换到深色界面';
      }
      storageSet(THEME_KEY, t);
    }

    function toggleTheme() {
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    }

    applyTheme(storageGet(THEME_KEY) || 'light');

    function renderMd(text) {
      let html = text
        .replace(/```json\n([\s\S]*?)```/g, '<pre>$1</pre>')
        .replace(/```\n([\s\S]*?)```/g, '<pre>$1</pre>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" target="_blank">$1</a>');
      return html;
    }

    function addMsg(text, who, meta) {
      const div = document.createElement('div');
      div.className = 'msg ' + who;
      div.innerHTML = renderMd(text);
      if (meta) {
        const m = document.createElement('div');
        m.className = 'meta';
        m.textContent = meta;
        div.appendChild(m);
      }
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
    }

    async function loadKb() {
      const r = await fetch('/api/kb');
      const j = await r.json();
      if (j.kb && j.kb.date) {
        kbInfo.textContent = '知识库：' + j.kb.date + ' · 更新 ' + (j.kb.updated_at || '');
      } else {
        kbInfo.textContent = '知识库未就绪，请先运行每日预测';
      }
    }

    async function send(text) {
      text = (text || input.value).trim();
      if (!text) return;
      input.value = '';
      addMsg(text, 'user');
      const heavy = /测一下|测今天|测今日|测当天|跑一遍|跑今天|重新预测|重新跑|更新预测|执行今日预测/.test(text);
      addMsg(heavy ? '正在跑今日预测（拉行情+新闻+大模型），约 1–3 分钟，请勿关闭窗口…' : '思考中…', 'bot');
      const pending = messages.lastChild;
      try {
        const r = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({message: text})
        });
        const j = await r.json();
        pending.remove();
        addMsg(j.reply || j.error || '无回复', 'bot');
        if (j.kb_saved || j.intent === 'run_today_job') loadKb();
      } catch (e) {
        pending.remove();
        addMsg('请求失败：' + e, 'bot');
      }
    }

    if (themeToggle) themeToggle.addEventListener('click', toggleTheme);
    document.getElementById('send').addEventListener('click', () => send());
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    document.querySelectorAll('.chip').forEach(el => {
      el.addEventListener('click', () => { input.value = el.dataset.q; send(el.dataset.q); });
    });

    loadKb();
    addMsg('你好。说「测一下今天」可生成持仓并附带上一日收益；收盘后可问「今天预测收益多少」。', 'bot');
  </script>
</body>
</html>"""


def _json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class AgentHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            data = CHAT_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/kb":
            qs = parse_qs(urlparse(self.path).query)
            date = (qs.get("date") or [None])[0]
            kb = load_knowledge_base(date)
            _json(self, {"kb": kb, "ok": kb is not None})
            return
        if path == "/api/rebuild_kb":
            qs = parse_qs(urlparse(self.path).query)
            date = (qs.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            try:
                path_out = rebuild_knowledge_base(str(date)[:10])
                _json(self, {"ok": True, "path": str(path_out)})
            except Exception as exc:
                _json(self, {"ok": False, "error": str(exc)}, status=400)
            return
        _json(self, {"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            body = json.loads(raw or "{}")
        except json.JSONDecodeError:
            body = {}

        if path == "/api/chat":
            msg = str(body.get("message") or "")
            date = body.get("date")
            result = handle_message(msg, date_str=date)
            _json(self, result)
            return
        _json(self, {"error": "not found"}, status=404)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF investment agent chat server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"ETF Agent: {url}")

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
    raise SystemExit(main())
