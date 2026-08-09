"""FastERP 3-pane layout — amber/operational palette, SSE AI rail."""
from __future__ import annotations

import os

from fasthtml.common import (
    Div, H1, H3, H4, P, Span, A, Button, Form, Input, Title, Link, Script, Style, NotStr,
)

LAYOUT_CSS = """
:root{
  --bg:#faf7f2; --surface:#ffffff; --surface-2:#f5efe6; --border:#ece2d4; --text:#27211a;
  --text-dim:#5b5246; --text-mute:#9a8f7f; --accent:#d97706; --accent-hover:#b45309;
  --accent-light:#fdeccb; --ok:#16a34a; --ok-light:#dcfce7; --warn:#ca8a04; --danger:#dc2626; --danger-light:#fee2e2;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;height:100%;background:var(--bg);color:var(--text);
  font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:14px;}
a{color:var(--accent);text-decoration:none;} a:hover{text-decoration:underline;}
.app{display:grid;grid-template-columns:230px 1fr var(--rail,340px);grid-template-rows:52px 1fr;
  grid-template-areas:"top top top" "left center right";height:100vh;overflow:hidden;transition:grid-template-columns .18s ease;}
.app.right-expanded{--rail:clamp(420px,42vw,720px);} .app.right-collapsed{--rail:0px;} .app.right-collapsed .right-pane{display:none;}
#copilot-reopen{position:fixed;right:0;bottom:26px;display:none;align-items:center;gap:6px;cursor:pointer;z-index:60;
  background:var(--accent);color:#fff;font-size:13px;font-weight:600;padding:9px 14px;border-radius:8px 0 0 8px;box-shadow:0 2px 10px rgba(0,0,0,.18);}
.app.right-collapsed #copilot-reopen{display:inline-flex;}
.copilot-min,.copilot-exp{cursor:pointer;border:1px solid var(--border);background:var(--surface);border-radius:6px;padding:4px 9px;font-size:13px;line-height:1;color:var(--text-mute);}
.topbar{grid-area:top;display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:var(--surface);border-bottom:1px solid var(--border);}
.brand{font-weight:700;letter-spacing:.3px;display:flex;align-items:center;gap:8px;font-size:16px;}
.brand-dot{width:11px;height:11px;background:var(--accent);border-radius:3px;display:inline-block;}
.env-pill{background:var(--accent-light);color:var(--accent-hover);padding:3px 10px;border-radius:999px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;}
.topbar .actions{display:flex;gap:10px;align-items:center;}
.left-pane{grid-area:left;background:var(--surface);border-right:1px solid var(--border);padding:12px 0;overflow-y:auto;}
.nav-section{margin-bottom:14px;} .nav-section h4{margin:6px 16px 4px;font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--text-mute);font-weight:700;}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 16px;color:var(--text-dim);cursor:pointer;border-left:3px solid transparent;}
.nav-item:hover{background:var(--surface-2);color:var(--text);text-decoration:none;}
.nav-item.active{background:var(--accent-light);color:var(--accent-hover);border-left-color:var(--accent);font-weight:600;}
.nav-icon{width:18px;display:inline-block;text-align:center;}
.center-pane{grid-area:center;overflow-y:auto;padding:20px 24px;}
.page-title{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.page-title h1{margin:0;font-size:22px;font-weight:700;} .page-title .sub{color:var(--text-mute);font-size:13px;margin-top:3px;}
.kpi-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px;}
.kpi{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px;position:relative;overflow:hidden;}
.kpi .label{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--text-mute);font-weight:600;}
.kpi .value{font-size:24px;font-weight:700;margin-top:4px;} .kpi .trend{font-size:12px;color:var(--text-mute);margin-top:2px;}
.kpi::after{content:'';position:absolute;top:0;right:0;bottom:0;width:4px;background:var(--accent);}
.kpi.ok::after{background:var(--ok);} .kpi.danger::after{background:var(--danger);}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;margin-bottom:16px;}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;} .card-header h3{margin:0;font-size:15px;font-weight:700;}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
table.tbl{width:100%;border-collapse:collapse;font-size:13px;}
table.tbl th{text-align:left;padding:8px 10px;background:var(--surface-2);color:var(--text-dim);font-weight:600;border-bottom:1px solid var(--border);}
table.tbl td{padding:8px 10px;border-bottom:1px solid var(--border);} table.tbl tr:last-child td{border-bottom:0;} table.tbl tr:hover td{background:var(--surface-2);}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;font-weight:600;background:var(--surface-2);color:var(--text-dim);white-space:nowrap;}
.pill.draft{background:#f1f5f9;color:#475569;} .pill.confirmed{background:var(--accent-light);color:var(--accent-hover);}
.pill.delivered{background:#dbeafe;color:#1d4ed8;} .pill.invoiced{background:#ede9fe;color:#6d28d9;}
.pill.closed,.pill.paid{background:var(--ok-light);color:#166534;} .pill.cancelled{background:#f1f5f9;color:#94a3b8;}
.pill.unpaid{background:var(--surface-2);color:#5b5246;} .pill.partlypaid{background:#fef3c7;color:#92400e;} .pill.overdue{background:var(--danger-light);color:#991b1b;}
.pill.low{background:var(--danger-light);color:#991b1b;} .pill.ok2{background:var(--ok-light);color:#166534;}
.pill.ordered{background:var(--accent-light);color:var(--accent-hover);} .pill.received{background:var(--ok-light);color:#166534;}
.funnel-row{display:grid;grid-template-columns:120px 1fr 110px;align-items:center;gap:10px;margin-bottom:7px;font-size:13px;}
.funnel-bar{height:18px;border-radius:5px;background:var(--accent);min-width:2px;} .funnel-row .v{text-align:right;color:var(--text-dim);}
.detail-grid{display:grid;grid-template-columns:1fr 300px;gap:16px;}
.kv{display:grid;grid-template-columns:130px 1fr;gap:6px 12px;font-size:13px;} .kv .k{color:var(--text-mute);}
.seg{display:inline-flex;gap:6px;margin-bottom:14px;flex-wrap:wrap;}
.seg a{padding:6px 12px;border:1px solid var(--border);border-radius:8px;color:var(--text-dim);background:var(--surface);font-size:13px;}
.seg a.active{background:var(--accent);color:#fff;border-color:var(--accent);}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:14px;flex-wrap:wrap;}
.toolbar input[type=search]{padding:8px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;min-width:240px;}
.o2c{display:flex;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:14px;}
.o2c-step{font-size:11.5px;padding:3px 10px;border-radius:999px;border:1px solid var(--border);color:var(--text-mute);background:var(--surface);}
.o2c-step.done{background:var(--accent-light);color:var(--accent-hover);border-color:var(--accent-light);font-weight:600;}
.o2c-arrow{color:var(--text-mute);font-size:12px;}
.inline-form{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.inline-form input{padding:7px 10px;border:1px solid var(--border);border-radius:8px;font-size:13px;}
.paid-note{margin-top:10px;color:var(--ok);font-weight:600;font-size:13px;}
.bar{height:8px;background:var(--surface-2);border-radius:4px;overflow:hidden;} .bar > span{display:block;height:100%;background:var(--accent);}
.btn{padding:6px 12px;border-radius:6px;border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;font-size:13px;}
.btn:hover{background:var(--surface-2);} .btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);} .btn.primary:hover{background:var(--accent-hover);} .btn.sm{padding:3px 9px;font-size:12px;}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:14px;}
.accounting-form input,.accounting-form select{width:100%;padding:9px 10px;border:1px solid var(--border);border-radius:7px;background:#fff;color:var(--text);}
.accounting-form .label{font-size:12px;font-weight:600;color:var(--text-dim);margin:0 0 5px;}
.form-actions{display:flex;gap:8px;margin-top:18px;}
.journal-line{display:grid;grid-template-columns:2fr 1fr 1fr 1.3fr 1.3fr;gap:8px;align-items:center;margin-bottom:8px;}
.journal-head{font-size:11px;color:var(--text-mute);text-transform:uppercase;letter-spacing:.5px;}
.summary-row,.setup-row{display:grid;grid-template-columns:1fr auto;gap:12px;padding:10px 0;border-bottom:1px solid var(--border);}
.summary-row.total{font-size:16px;border-top:2px solid var(--border);border-bottom:0;}
.setup-row{grid-template-columns:minmax(55px,.5fr) 1.5fr 1fr;font-size:13px;}
.project-row{padding:8px 0;border-bottom:1px solid var(--border);}
.project-row>div{display:flex;align-items:center;justify-content:space-between;}
.grid-3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;}
.login-wrap{height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(135deg,#f6ead4 0%,#fdeccb 100%);}
.login-card{background:#fff;padding:36px 40px;border-radius:14px;width:360px;box-shadow:0 20px 40px rgba(15,23,42,.08);}
.login-card h1{margin:0 0 4px;font-size:22px;} .login-card p{margin:0 0 20px;color:var(--text-mute);font-size:13px;}
.login-card input{width:100%;padding:10px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:10px;font-size:14px;}
.login-card button{width:100%;padding:10px;font-weight:600;} .login-card .error{color:var(--danger);font-size:12px;margin:6px 0;} .login-card .hint{font-size:11.5px;color:var(--text-mute);margin-top:10px;text-align:center;}
.right-pane{grid-area:right;background:var(--surface);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
.right-header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;} .right-header h3{margin:0;font-size:14px;font-weight:700;} .right-header .tabs{display:flex;gap:6px;}
.chat-body{flex:1;overflow-y:auto;padding:14px 16px;display:flex;flex-direction:column;gap:12px;}
.msg{max-width:90%;padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.55;overflow-wrap:anywhere;}
.msg.user{background:var(--accent);color:#fff;align-self:flex-end;border-bottom-right-radius:3px;white-space:pre-wrap;}
.msg.assistant{background:var(--surface-2);border:1px solid var(--border);color:var(--text);align-self:flex-start;border-bottom-left-radius:3px;}
.msg table{width:100%;table-layout:fixed;font-size:11.5px;border-collapse:collapse;border:1px solid var(--border);margin:6px 0;}
.msg th{background:var(--text);color:#fff;font-size:10.5px;} .msg th,.msg td{text-align:left;padding:5px 7px;border:1px solid var(--border);overflow-wrap:anywhere;}
.msg code{background:rgba(0,0,0,.06);padding:1px 4px;border-radius:3px;font-size:12px;}
.chat-input{border-top:1px solid var(--border);padding:10px;background:var(--surface);} .chat-input-row{display:flex;gap:8px;align-items:stretch;}
.chat-input-row input{flex:1;min-width:0;padding:10px 12px;border:1px solid var(--border);border-radius:8px;font-size:13px;outline:none;}
.chat-input-row input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-light);}
.chat-send-btn{display:inline-flex;align-items:center;background:var(--accent);color:#fff;border:none;border-radius:8px;padding:0 16px;font-weight:600;font-size:13px;cursor:pointer;} .chat-send-btn:disabled{background:var(--text-mute);}
.chat-empty-hint{color:var(--text-mute);font-size:12.5px;line-height:1.5;text-align:center;padding:18px 14px;}
.sample-cards{padding:.4rem 1rem .8rem;background:var(--surface);border-top:1px solid var(--border);}
.sample-cards-label{display:inline-block;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.12em;color:var(--text-mute);margin-bottom:6px;}
.sample-card{display:flex;align-items:center;gap:8px;background:var(--bg);border:1px solid var(--border);padding:9px 12px;border-radius:10px;font-size:12.5px;cursor:pointer;color:var(--text-dim);width:100%;text-align:left;line-height:1.35;margin-bottom:6px;font-family:inherit;}
.sample-card::before{content:"💬";flex-shrink:0;} .sample-card:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-light);}
.thinking-indicator{display:flex;align-items:center;gap:8px;padding:6px 14px;font-size:12.5px;color:var(--text-mute);align-self:flex-start;}
.thinking-indicator .dot{width:8px;height:8px;border-radius:50%;background:var(--accent);animation:pulse 1.2s ease-in-out infinite;}
@keyframes pulse{0%,100%{opacity:.35;transform:scale(.85);}50%{opacity:1;transform:scale(1.1);}}
"""

NAV_ITEMS = [
    ("OVERVIEW", [("dashboard", "Dashboard", "📊", "/"), ("ai", "AI Assistant", "🤖", "/ai")]),
    ("SELLING", [("orders", "Sales Orders", "📦", "/orders"),
                 ("invoices", "Invoices (AR)", "🧾", "/invoices"),
                 ("customers", "Customers", "🏢", "/customers")]),
    ("BUYING", [("suppliers", "Suppliers", "🏭", "/suppliers"),
                ("purchase", "Purchase Orders", "🛒", "/purchase")]),
    ("STOCK", [("items", "Items & Stock", "📋", "/items")]),
    ("ACCOUNTING", [("accounting", "Overview", "🧮", "/accounting"),
                    ("accounts", "Chart of Accounts", "🗂️", "/accounting/accounts"),
                    ("expenses", "Expenses", "💳", "/accounting/expenses"),
                    ("journals", "Journal Entries", "📝", "/accounting/journals"),
                    ("ledger", "General Ledger", "📚", "/ledger"),
                    ("projects", "Projects", "🎯", "/accounting/projects"),
                    ("reports", "Reports", "📈", "/accounting/reports"),
                    ("accounting_setup", "Setup", "⚙️", "/accounting/setup")]),
    ("HELP", [("guide", "User Guide", "📖", "/guide"),
              ("developers", "Developers", "⌘", "/developers")]),
]
SAMPLE_QUESTIONS = ["What's outstanding from customers?", "Which items are low on stock?", "How are sales orders tracking?"]


def topbar(env, user_email):
    right = Div(
        Button(NotStr("&laquo; Chat"), id="copilot-topbar-toggle", cls="btn", onclick="toggleCopilot()") if user_email else None,
        Span(env, cls="env-pill"),
        Span(user_email or "", style="color:var(--text-mute);font-size:12px;") if user_email else None,
        A("Logout", href="/logout", cls="btn") if user_email else None, cls="actions")
    return Div(Div(Span(cls="brand-dot"), Span("Fast", style="font-weight:800;"),
                   Span("ERP", style="color:var(--accent);font-weight:700;letter-spacing:.5px;"), cls="brand"),
               right, cls="topbar")


def left_pane(active):
    sections = []
    for name, items in NAV_ITEMS:
        links = [A(Span(icon, cls="nav-icon"), Span(label), href=href,
                   cls=f"nav-item {'active' if active == key else ''}") for key, label, icon, href in items]
        sections.append(Div(H4(name), *links, cls="nav-section"))
    return Div(*sections, cls="left-pane")


def _sample_cards():
    cards = [Button(Span(q), cls="sample-card", onclick=f"fillChat({q!r});sendMessage(null);", title=q) for q in SAMPLE_QUESTIONS]
    return Div(Div(Span("Try asking:", cls="sample-cards-label")), Div(*cards), cls="sample-cards")


def right_pane_chat(thread_id):
    return Div(
        Div(H3("AI Assistant"),
            Div(Button("New", cls="btn", hx_get="/chat/new", hx_target="#chat-body", hx_swap="innerHTML"),
                Button(NotStr("&laquo;"), id="copilot-exp-btn", cls="copilot-exp", onclick="toggleExpand()"),
                Button(NotStr("&rsaquo;"), cls="copilot-min", onclick="toggleCopilot()"), cls="tabs"),
            cls="right-header"),
        Div(Div(P("Ask about sales, receivables or stock — or use /sales /ar /stock /help.",
                  cls="chat-empty-hint"), id="chat-body", cls="chat-body"),
            Form(Input(type="hidden", name="thread_id", value=thread_id, id="thread-id"),
                 Div(Input(type="text", name="message", id="chat-input",
                           placeholder="Ask about your ERP or /ar /stock /help …", autocomplete="off"),
                     Button("Send", type="submit", cls="chat-send-btn", id="chat-send-btn"), cls="chat-input-row"),
                 onsubmit="return streamChat(event)", cls="chat-input"),
            _sample_cards(),
            style="display:flex;flex-direction:column;flex:1;overflow:hidden;"),
        cls="right-pane")


def page(active, env, user_email, thread_id, *content, right_override=None):
    right = right_override if right_override is not None else right_pane_chat(thread_id)
    return (Title("FastERP"),
            Link(rel="icon", type="image/svg+xml", href="/static/favicon.svg"),
            Script(src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"),
            Style(LAYOUT_CSS),
            Div(topbar(env, user_email), left_pane(active), Div(*content, cls="center-pane"), right,
                Div(NotStr("&lsaquo; AI Assistant"), id="copilot-reopen", onclick="toggleCopilot()"), cls="app"),
            Script(LAYOUT_JS))


def kpi_card(label, value, trend="", tone=""):
    return Div(Div(label, cls="label"),
               Div(f"{value:,}" if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value), cls="value"),
               Div(trend, cls="trend") if trend else None, cls=f"kpi {tone}")


def money(v):
    v = v or 0
    currency = os.getenv("FASTERP_DISPLAY_CURRENCY", "").strip().upper()
    if not currency:
        currency = os.getenv("FASTERP_COMPANY_CODE", "DEMO-GBP").rsplit("-", 1)[-1]
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency, f"{currency} ")
    return (f"{symbol}{v/1_000_000:.2f}M" if v >= 1_000_000
            else f"{symbol}{v/1_000:.0f}k" if v >= 1_000
            else f"{symbol}{v:,.0f}")


LAYOUT_JS = """
function _sync(){var app=document.querySelector('.app');if(!app)return;
  var ex=app.classList.contains('right-expanded'),col=app.classList.contains('right-collapsed');
  var eb=document.getElementById('copilot-exp-btn');if(eb){eb.innerHTML=ex?'\\u00BB':'\\u00AB';}
  var tb=document.getElementById('copilot-topbar-toggle');if(tb){tb.innerHTML=col?'\\u00AB Chat':'Chat \\u203A';}}
function toggleCopilot(){var app=document.querySelector('.app');if(!app)return;app.classList.toggle('right-collapsed');
  if(app.classList.contains('right-collapsed'))app.classList.remove('right-expanded');
  try{localStorage.setItem('erpCollapsed',app.classList.contains('right-collapsed')?'1':'0');}catch(e){}_sync();}
function toggleExpand(){var app=document.querySelector('.app');if(!app)return;app.classList.remove('right-collapsed');app.classList.toggle('right-expanded');
  try{localStorage.setItem('erpExpanded',app.classList.contains('right-expanded')?'1':'0');localStorage.setItem('erpCollapsed','0');}catch(e){}_sync();}
(function(){try{var app=document.querySelector('.app');if(!app)return;
  if(localStorage.getItem('erpCollapsed')==='1')app.classList.add('right-collapsed');
  else if(localStorage.getItem('erpExpanded')==='1')app.classList.add('right-expanded');}catch(e){}})();
document.addEventListener('DOMContentLoaded',_sync);
function fillChat(t){var el=document.getElementById('chat-input');if(el){el.value=t;el.focus();}}
function sendMessage(ev){return streamChat(ev);}
var _streaming=false,_thinker=null;
function _esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function _md(t){try{return marked.parse(t);}catch(e){return _esc(t);}}
function _scroll(){var cb=document.getElementById('chat-body');if(cb)cb.scrollTop=cb.scrollHeight;}
function addBubble(role,html){var cb=document.getElementById('chat-body');if(!cb)return null;
  var h=cb.querySelector('.chat-empty-hint');if(h)h.style.display='none';
  var d=document.createElement('div');d.className='msg '+role;d.innerHTML=html||'';cb.appendChild(d);_scroll();return d;}
function showThinking(){var cb=document.getElementById('chat-body');if(!cb)return;
  _thinker={el:document.createElement('div')};_thinker.el.className='thinking-indicator';
  _thinker.el.innerHTML='<span class="dot"></span> Thinking…';cb.appendChild(_thinker.el);_scroll();}
function hideThinking(){if(_thinker){if(_thinker.el.parentNode)_thinker.el.parentNode.removeChild(_thinker.el);_thinker=null;}}
async function streamChat(ev){if(ev&&ev.preventDefault)ev.preventDefault();if(_streaming)return false;
  var input=document.getElementById('chat-input');var msg=input?input.value.trim():'';if(!msg)return false;
  _streaming=true;var btn=document.getElementById('chat-send-btn');if(btn)btn.disabled=true;
  addBubble('user',_esc(msg));input.value='';
  var tid=(document.getElementById('thread-id')||{}).value||'';var bubble=null,acc='';showThinking();
  try{var resp=await fetch('/chat/stream',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},
    body:new URLSearchParams({message:msg,thread_id:tid})});
    if(!resp.ok){hideThinking();addBubble('assistant','Error: '+resp.status);_streaming=false;if(btn)btn.disabled=false;return false;}
    var reader=resp.body.getReader(),dec=new TextDecoder(),buf='';
    while(true){var r=await reader.read();if(r.done)break;buf+=dec.decode(r.value,{stream:true});
      var idx;while((idx=buf.indexOf('\\n\\n'))!==-1){var raw=buf.slice(0,idx);buf=buf.slice(idx+2);
        if(raw.indexOf('data: ')!==0)continue;var p={};try{p=JSON.parse(raw.slice(6));}catch(e){}
        if(p.token){if(acc===''){hideThinking();bubble=addBubble('assistant','');}acc+=p.token;bubble.innerHTML=_md(acc);_scroll();}
        else if(p.error){hideThinking();addBubble('assistant','⚠ '+p.error);}}}
  }catch(e){hideThinking();addBubble('assistant','⚠ '+e);}
  hideThinking();_streaming=false;if(btn)btn.disabled=false;return false;}
"""
