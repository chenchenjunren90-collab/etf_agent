"""ETF investment agent — conversational UI + API (port 8766).

Supports:
  - multi-turn session + info collection (choices / fill-in)
  - personal advice scaled to user capital
  - competition JSON output via daily_job
  - news / why / boundary guards
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_kb import load_knowledge_base, rebuild_knowledge_base
from agent_orchestrator import handle_chat, start_session
import security_guard
import session_store as store

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
      --btn-fg:#001018; --ok:#34d399; --warn:#fbbf24; --card:#0f1a2e;
      --danger:#f87171;
    }
    html[data-theme="light"] {
      --bg:#f1f5f9; --panel:#ffffff; --line:#cbd5e1;
      --text:#0f172a; --muted:#64748b; --accent:#0284c7; --user:#2563eb;
      --chip-bg:#f8fafc; --input-bg:#ffffff; --aside-bg:rgba(248,250,252,.95);
      --btn-fg:#f8fafc; --ok:#059669; --warn:#d97706; --card:#f8fafc;
      --danger:#dc2626;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;background:var(--bg);color:var(--text);height:100vh;display:flex;flex-direction:column;transition:background .2s,color .2s}
    header{position:relative;z-index:20;padding:14px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;background:var(--bg)}
    h1{margin:0;font-size:20px;letter-spacing:.02em}
    .sub{color:var(--muted);font-size:13px}
    .header-right{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
    .theme-btn,.ghost-btn{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:10px;padding:8px 14px;background:var(--panel);color:var(--text);font-size:13px;font-weight:500;cursor:pointer;white-space:nowrap;text-decoration:none}
    .theme-btn:hover,.ghost-btn:hover{border-color:var(--accent);color:var(--accent)}
    main{flex:1;display:grid;grid-template-columns:280px 1fr;min-height:0}
    aside{border-right:1px solid var(--line);padding:14px;overflow:auto;background:var(--aside-bg)}
    aside h3{margin:0 0 10px;font-size:14px;color:var(--muted)}
    .chip{display:block;padding:8px 10px;margin-bottom:8px;border:1px solid var(--line);border-radius:10px;background:var(--chip-bg);cursor:pointer;font-size:13px;color:var(--text)}
    .chip:hover{border-color:var(--accent)}
    #chat{display:flex;flex-direction:column;min-height:0;background:var(--bg)}
    #messages{flex:1;overflow:auto;padding:18px 20px;display:flex;flex-direction:column;gap:14px}
    .msg{max-width:min(720px,92%);padding:12px 14px;border-radius:14px;line-height:1.6;font-size:14px}
    .bot{align-self:flex-start;background:var(--panel);border:1px solid var(--line);color:var(--text)}
    .user{align-self:flex-end;background:var(--user);color:#f8fafc;white-space:pre-wrap}
    .meta{font-size:11px;color:var(--muted);margin-top:6px}
    .blocks{margin-top:12px;display:flex;flex-direction:column;gap:10px}
    .q{font-weight:600;margin-bottom:6px}
    .hint{font-size:12px;color:var(--muted);margin-bottom:8px}
    .opts{display:flex;flex-wrap:wrap;gap:8px}
    .opt-btn{border:1px solid var(--line);background:var(--chip-bg);color:var(--text);border-radius:999px;padding:8px 14px;cursor:pointer;font-size:13px}
    .opt-btn:hover{border-color:var(--accent);color:var(--accent)}
    .opt-btn:disabled{opacity:.5;cursor:default}
    .field-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
    .field-row input{flex:1;min-width:140px;border:1px solid var(--line);border-radius:10px;background:var(--input-bg);color:var(--text);padding:8px 10px;font-size:14px}
    .field-row button{border:0;border-radius:10px;padding:8px 14px;background:var(--accent);color:var(--btn-fg);font-weight:600;cursor:pointer}
    .advice-card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
    .advice-card h4{margin:0 0 8px;font-size:14px}
    .hold-row{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:13px}
    .hold-row:last-child{border-bottom:0}
    .tag{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
    .json-box{position:relative;background:var(--input-bg);border:1px solid var(--line);border-radius:10px;padding:10px;font-family:ui-monospace,Consolas,monospace;font-size:12px;overflow:auto;max-height:240px;white-space:pre}
    .copy-btn{margin-top:8px;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:8px;padding:6px 10px;cursor:pointer;font-size:12px}
    .disclaimer{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.4}
    #composer{border-top:1px solid var(--line);padding:12px 16px;display:flex;gap:10px;background:var(--bg);align-items:flex-end}
    textarea{flex:1;min-height:52px;max-height:120px;resize:vertical;border:1px solid var(--line);border-radius:12px;background:var(--input-bg);color:var(--text);padding:10px 12px;font-size:14px}
    #send{border:0;border-radius:10px;padding:10px 16px;background:var(--accent);color:var(--btn-fg);font-weight:600;cursor:pointer;height:44px}
    pre{background:var(--input-bg);border:1px solid var(--line);padding:10px;border-radius:8px;overflow:auto;font-size:12px;color:var(--text)}
    a{color:var(--accent)}
    .footer-bar{padding:8px 16px;border-top:1px solid var(--line);font-size:11px;color:var(--muted);text-align:center}
    @media(max-width:800px){main{grid-template-columns:1fr}aside{display:none}}
  </style>
</head>
<body data-theme="light">
  <header>
    <div>
      <h1>ETF 投资智能体</h1>
      <div class="sub">对话式建议 · 信息收集 · 比赛提交 · 新闻解读</div>
    </div>
    <div class="header-right">
      <div class="sub" id="kbInfo">知识库加载中…</div>
      <div class="sub" id="sessInfo"></div>
      <a class="ghost-btn" id="dashboardLink" href="/etf-agent/" title="查看并复制当日比赛投资建议">Dashboard</a>
      <button type="button" class="ghost-btn" id="newSession" title="新会话">新会话</button>
      <button type="button" class="theme-btn" id="themeToggle">黑夜模式</button>
    </div>
  </header>
  <main>
    <aside>
      <h3>快捷入口</h3>
      <div class="chip" data-q="今日投资建议">今日投资建议</div>
      <div class="chip" data-q="今日比赛预测">今日比赛预测</div>
      <div class="chip" data-q="今日比赛提交格式">比赛提交 JSON</div>
      <div class="chip" data-q="测一下今天">测一下今天预测</div>
      <div class="chip" data-q="为什么选这些ETF">为什么选这些 ETF</div>
      <div class="chip" data-q="今日筛选后的新闻有哪些">今日筛选新闻</div>
      <div class="chip" data-q="今天预测的收益是多少">今日盘后收益</div>
      <div class="chip" data-q="昨天赚了多少钱">昨天收益复盘</div>
      <h3 style="margin-top:16px">产品边界</h3>
      <p class="sub" style="margin:0;line-height:1.5">只服务 A 股 ETF。个股、无关话题会引导回正题。比赛模式固定 50 万本金。个人建议只读比赛结果，不会改写每日官方预测。</p>
    </aside>
    <section id="chat">
      <div id="messages"></div>
      <div id="composer">
        <textarea id="input" placeholder="例如：今日投资建议 / 今日比赛预测 / 为什么买红利ETF / 某新闻对证券ETF有何影响"></textarea>
        <button id="send">发送</button>
      </div>
    </section>
  </main>
  <div class="footer-bar">仅供参考，不构成投资建议 · 市场有风险，决策需自负</div>
  <script>
    const messages = document.getElementById('messages');
    const input = document.getElementById('input');
    const kbInfo = document.getElementById('kbInfo');
    const sessInfo = document.getElementById('sessInfo');
    const themeToggle = document.getElementById('themeToggle');
    const THEME_KEY = 'etf-agent-theme';
    const SESS_KEY = 'etf-agent-session';
    let sessionId = null;
    let busy = false;

    // Support both local (:8766/) and nginx (/etf-agent/chat/)
    const API_BASE = (() => {
      const p = window.location.pathname || '/';
      if (p.indexOf('/etf-agent/chat') === 0) return '/etf-agent/chat';
      return '';
    })();
    function apiUrl(path) {
      return API_BASE + path;
    }

    function storageGet(key) {
      try { return localStorage.getItem(key); } catch (e) { return null; }
    }
    function storageSet(key, val) {
      try { localStorage.setItem(key, val); } catch (e) {}
    }

    async function copyText(text) {
      if (navigator.clipboard && window.location.protocol === 'https:') {
        try {
          await navigator.clipboard.writeText(text);
          return true;
        } catch (e) {}
      }
      const helper = document.createElement('textarea');
      helper.value = text;
      helper.setAttribute('readonly', '');
      helper.style.position = 'fixed';
      helper.style.opacity = '0';
      document.body.appendChild(helper);
      helper.focus();
      helper.select();
      helper.setSelectionRange(0, helper.value.length);
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      helper.remove();
      return ok;
    }

    const dashboardLink = document.getElementById('dashboardLink');
    if (dashboardLink && API_BASE === '') {
      dashboardLink.href = window.location.protocol + '//' + window.location.hostname + ':8765/';
    }

    function applyTheme(theme) {
      const t = theme === 'light' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', t);
      document.body.setAttribute('data-theme', t);
      if (themeToggle) {
        themeToggle.textContent = t === 'dark' ? '白天模式' : '黑夜模式';
      }
      storageSet(THEME_KEY, t);
    }
    applyTheme(storageGet(THEME_KEY) || 'light');

    function renderMd(text) {
      if (!text) return '';
      let html = String(text)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      html = html
        .replace(/```json\n([\s\S]*?)```/g, '<pre>$1</pre>')
        .replace(/```\n([\s\S]*?)```/g, '<pre>$1</pre>')
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        .replace(/\[(.+?)\]\((.+?)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
        .replace(/\n/g, '<br>');
      return html;
    }

    function updateSessionBadge(sess) {
      if (!sess) return;
      sessionId = sess.session_id;
      storageSet(SESS_KEY, sessionId);
      const cap = sess.profile && sess.profile.capital;
      const risk = sess.profile && sess.profile.risk_preference;
      let t = '会话 ' + String(sessionId).slice(0,6);
      if (cap) t += ' · 资金 ' + Number(cap).toLocaleString();
      if (risk) t += ' · ' + risk;
      sessInfo.textContent = t;
    }

    function addMsg(text, who, meta) {
      const div = document.createElement('div');
      div.className = 'msg ' + who;
      if (who === 'user') {
        div.textContent = text;
      } else {
        div.innerHTML = renderMd(text);
      }
      if (meta) {
        const m = document.createElement('div');
        m.className = 'meta';
        m.textContent = meta;
        div.appendChild(m);
      }
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
      return div;
    }

    function disableBlockButtons(root) {
      root.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
    }

    function renderBlocks(blocks, hostMsg) {
      if (!blocks || !blocks.length) return;
      const wrap = document.createElement('div');
      wrap.className = 'blocks';

      blocks.forEach(block => {
        const box = document.createElement('div');
        if (block.type === 'choices' || block.type === 'choices_or_input') {
          if (block.question) {
            const q = document.createElement('div');
            q.className = 'q';
            q.textContent = block.question;
            box.appendChild(q);
          }
          if (block.hint) {
            const h = document.createElement('div');
            h.className = 'hint';
            h.textContent = block.hint;
            box.appendChild(h);
          }
          const opts = document.createElement('div');
          opts.className = 'opts';
          (block.options || []).forEach(opt => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'opt-btn';
            btn.textContent = opt.label;
            btn.addEventListener('click', () => {
              disableBlockButtons(wrap);
              if (block.field === 'entry') {
                send(String(opt.value));
              } else if (opt.value === '__custom__') {
                // focus custom input if present
                const inp = wrap.querySelector('input[data-field]');
                if (inp) inp.focus();
                else sendField(block.field, '__custom__');
              } else {
                sendField(block.field, opt.value, opt.label);
              }
            });
            opts.appendChild(btn);
          });
          box.appendChild(opts);
          if (block.type === 'choices_or_input' || block.input_type) {
            const row = document.createElement('div');
            row.className = 'field-row';
            const inp = document.createElement('input');
            inp.type = block.input_type === 'number' ? 'number' : 'text';
            inp.placeholder = block.placeholder || '自定义输入';
            inp.setAttribute('data-field', block.field);
            if (block.min != null) inp.min = block.min;
            if (block.max != null) inp.max = block.max;
            const ok = document.createElement('button');
            ok.type = 'button';
            ok.textContent = '确认';
            ok.addEventListener('click', () => {
              disableBlockButtons(wrap);
              sendField(block.field, inp.value, inp.value);
            });
            inp.addEventListener('keydown', e => {
              if (e.key === 'Enter') { e.preventDefault(); ok.click(); }
            });
            row.appendChild(inp);
            row.appendChild(ok);
            box.appendChild(row);
          }
        } else if (block.type === 'advice_card') {
          box.className = 'advice-card';
          const title = document.createElement('h4');
          const mode = block.risk_preference === 'competition' ? '比赛持仓' : '个人配置建议';
          title.textContent = (block.date || '') + ' · ' + mode;
          box.appendChild(title);
          const tags = document.createElement('div');
          tags.style.margin = '6px 0 8px';
          if (block.capital) {
            const tag = document.createElement('span');
            tag.className = 'tag';
            tag.textContent = '资金 ' + Number(block.capital).toLocaleString() + ' 元';
            tags.appendChild(tag);
          }
          if (block.risk_preference && block.risk_preference !== 'competition') {
            const tag = document.createElement('span');
            tag.className = 'tag';
            tag.style.marginLeft = '6px';
            const riskMap = {conservative:'稳健', balanced:'均衡', aggressive:'进取'};
            tag.textContent = riskMap[block.risk_preference] || block.risk_preference;
            tags.appendChild(tag);
          }
          if (block.focus) {
            const tag = document.createElement('span');
            tag.className = 'tag';
            tag.style.marginLeft = '6px';
            const focusMap = {auto:'跟随策略', dividend:'防守红利', broad:'宽基', growth:'成长', sector:'行业'};
            tag.textContent = focusMap[block.focus] || block.focus;
            tags.appendChild(tag);
          }
          box.appendChild(tags);
          if (block.is_empty) {
            const p = document.createElement('p');
            p.textContent = '今日建议空仓';
            box.appendChild(p);
          } else {
            (block.holdings || []).forEach(h => {
              const row = document.createElement('div');
              row.className = 'hold-row';
              const left = document.createElement('div');
              left.innerHTML = '<strong>' + (h.symbol_name || h.symbol) + '</strong> <span class="sub">' + (h.symbol || '') + '</span>';
              const right = document.createElement('div');
              let t = (h.volume || 0).toLocaleString() + ' 股';
              if (h.approx_amount) t += ' · 约 ' + Number(h.approx_amount).toLocaleString() + ' 元';
              if (h.weight_pct != null) t += ' (' + h.weight_pct + '%)';
              right.textContent = t;
              row.appendChild(left);
              row.appendChild(right);
              box.appendChild(row);
            });
          }
          if (block.risk_note) {
            const n = document.createElement('div');
            n.className = 'hint';
            n.style.marginTop = '8px';
            n.textContent = block.risk_note.replace(/\*\*/g, '');
            box.appendChild(n);
          }
          if (block.disclaimer) {
            const d = document.createElement('div');
            d.className = 'disclaimer';
            d.textContent = block.disclaimer;
            box.appendChild(d);
          }
        } else if (block.type === 'json_block') {
          const title = document.createElement('div');
          title.className = 'q';
          title.textContent = block.title || 'JSON';
          box.appendChild(title);
          if (block.hint) {
            const h = document.createElement('div');
            h.className = 'hint';
            h.textContent = block.hint;
            box.appendChild(h);
          }
          const pre = document.createElement('div');
          pre.className = 'json-box';
          const text = JSON.stringify(block.data || [], null, 2);
          pre.textContent = text;
          box.appendChild(pre);
          const copy = document.createElement('button');
          copy.type = 'button';
          copy.className = 'copy-btn';
          copy.textContent = '复制当日投资建议';
          copy.addEventListener('click', async () => {
            const ok = await copyText(text);
            copy.textContent = ok ? '已复制' : '复制失败，请手动选取';
            setTimeout(() => copy.textContent = '复制当日投资建议', 1500);
          });
          box.appendChild(copy);
        }
        wrap.appendChild(box);
      });

      hostMsg.appendChild(wrap);
      messages.scrollTop = messages.scrollHeight;
    }

    async function loadKb() {
      try {
        const r = await fetch(apiUrl('/api/kb'));
        const j = await r.json();
        if (j.kb && j.kb.date) {
          kbInfo.textContent = '知识库：' + j.kb.date + ' · 更新 ' + (j.kb.updated_at || '');
        } else {
          kbInfo.textContent = '知识库未就绪，可先「测一下今天」';
        }
      } catch (e) {
        kbInfo.textContent = '知识库加载失败';
      }
    }

    async function initSession(forceNew) {
      if (!forceNew) {
        sessionId = storageGet(SESS_KEY);
      } else {
        sessionId = null;
        storageSet(SESS_KEY, '');
      }
      const r = await fetch(apiUrl('/api/session/start'), {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(forceNew || !sessionId ? {} : {session_id: sessionId})
      });
      const j = await r.json();
      if (j.session) updateSessionBadge(j.session);
      messages.innerHTML = '';
      const bot = addMsg(j.reply || '你好。', 'bot', j.intent || 'greeting');
      renderBlocks(j.ui_blocks || [], bot);
    }

    async function sendField(field, value, displayLabel) {
      const label = displayLabel != null ? String(displayLabel) : String(value);
      addMsg(label, 'user');
      await postChat({
        message: label,
        field_answer: { field: field, value: value }
      });
    }

    async function send(text) {
      text = (text || input.value).trim();
      if (!text || busy) return;
      input.value = '';
      addMsg(text, 'user');
      await postChat({ message: text });
    }

    async function postChat(payload) {
      busy = true;
      const heavy = /测一下|跑一遍|重新预测|执行今日|生成今日|今日投资建议|今天投资建议|改成|换成|重新配/.test(payload.message || '');
      const pending = addMsg(heavy ? '正在用基础数据现算建议（行情+新闻+策略），约数十秒，请稍候…' : '思考中…', 'bot');
      try {
        const body = Object.assign({ session_id: sessionId }, payload);
        const r = await fetch(apiUrl('/api/chat'), {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(body)
        });
        if (!r.ok) {
          const t = await r.text();
          throw new Error('HTTP ' + r.status + ' ' + t.slice(0, 80));
        }
        const j = await r.json();
        pending.remove();
        if (j.session) updateSessionBadge(j.session);
        const bot = addMsg(j.reply || j.error || '无回复', 'bot', (j.intent || '') + (j.via ? ' · ' + j.via : ''));
        renderBlocks(j.ui_blocks || [], bot);
        if (j.kb_saved || j.intent === 'run_today_job' || j.intent === 'personal_advice' || j.intent === 'competition') {
          loadKb();
        }
      } catch (e) {
        pending.remove();
        addMsg('请求失败：' + e, 'bot');
      } finally {
        busy = false;
      }
    }

    themeToggle.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      applyTheme(cur === 'dark' ? 'light' : 'dark');
    });
    document.getElementById('newSession').addEventListener('click', () => initSession(true));
    document.getElementById('send').addEventListener('click', () => send());
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
    });
    document.querySelectorAll('.chip').forEach(el => {
      el.addEventListener('click', () => send(el.dataset.q));
    });

    loadKb();
    initSession(false);
  </script>
</body>
</html>"""


def _json(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    try:
        body = json.loads(raw or "{}")
    except json.JSONDecodeError:
        body = {}
    return body if isinstance(body, dict) else {}


class AgentHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        # Support reverse-proxy prefix /etf-agent/chat/
        if path in ("/", "/index.html", "/etf-agent/chat", "/etf-agent/chat/"):
            data = CHAT_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/docs", "/docs.html", "/etf-agent/chat/docs", "/etf-agent/chat/docs.html"):
            docs_path = Path(__file__).resolve().parent / "docs.html"
            if not docs_path.exists():
                self.send_error(404, "docs.html missing")
                return
            data = docs_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/api/kb", "/etf-agent/chat/api/kb"):
            qs = parse_qs(urlparse(self.path).query)
            date = (qs.get("date") or [None])[0]
            kb = load_knowledge_base(date)
            _json(self, {"kb": kb, "ok": kb is not None})
            return
        if path in ("/api/rebuild_kb", "/etf-agent/chat/api/rebuild_kb"):
            blocked = security_guard.check_admin_action(self)
            if blocked:
                _json(self, blocked, status=403)
                return
            qs = parse_qs(urlparse(self.path).query)
            date = (qs.get("date") or [datetime.now().strftime("%Y-%m-%d")])[0]
            try:
                path_out = rebuild_knowledge_base(str(date)[:10])
                _json(self, {"ok": True, "path": str(path_out)})
            except Exception as exc:
                _json(self, {"ok": False, "error": str(exc)}, status=400)
            return
        if path.startswith("/api/session/") or path.startswith("/etf-agent/chat/api/session/"):
            sid = path.rstrip("/").split("/")[-1]
            if sid and sid not in ("session", "start"):
                sess = store.get_session(sid)
                if not sess:
                    _json(self, {"ok": False, "error": "session not found"}, status=404)
                    return
                _json(self, {"ok": True, "session": store.public_view(sess)})
                return
        _json(self, {"error": "not found"}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = _read_body(self)

        if path in ("/api/session/start", "/etf-agent/chat/api/session/start"):
            # Always create a fresh welcome session for the UI bootstrap.
            # Client may pass old id only for display; we mint new for clarity.
            result = start_session()
            _json(self, result)
            return

        if path in ("/api/chat", "/etf-agent/chat/api/chat"):
            blocked = security_guard.check_chat(self)
            if blocked:
                _json(self, blocked, status=429)
                return
            msg = str(body.get("message") or "")
            date = body.get("date")
            sid = body.get("session_id")
            field_answer = body.get("field_answer")
            if field_answer is not None and not isinstance(field_answer, dict):
                field_answer = None
            try:
                result = handle_chat(
                    msg,
                    session_id=str(sid) if sid else None,
                    date_str=date,
                    field_answer=field_answer,
                )
                _json(self, result)
            except Exception as exc:
                _json(self, {"error": str(exc), "reply": f"服务异常：{exc}", "intent": "error"}, status=500)
            return

        _json(self, {"error": "not found"}, status=404)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {fmt % args}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF investment agent chat server")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AgentHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"ETF Agent Chat: {url}")
    print(f"Listening on {args.host}:{args.port}")

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
    raise SystemExit(main())
