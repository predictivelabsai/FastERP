"""Shared FastSME local-account authentication and CarHero-style modal."""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fasthtml.common import *
from starlette.responses import JSONResponse, RedirectResponse

AUTH_CSS = """
.auth-overlay{position:fixed;inset:0;z-index:1000;background:rgba(17,24,39,.46);display:none;align-items:center;justify-content:center;padding:20px}
.auth-overlay.visible{display:flex}.auth-dialog{width:min(400px,100%);max-height:calc(100vh - 40px);overflow:auto;background:#fff;border:1px solid #e5e7eb;border-radius:18px;padding:24px;box-shadow:0 24px 70px rgba(15,23,42,.22);position:relative}
.auth-close{position:absolute;right:16px;top:14px;border:0;background:transparent;font-size:24px;color:#6b7280;cursor:pointer}
.auth-tabs{display:flex;border-bottom:1px solid #e5e7eb;margin-bottom:20px}.auth-tab{flex:1;border:0;background:transparent;padding:10px 8px;color:#6b7280;font-weight:650;cursor:pointer;border-bottom:2px solid transparent}
.auth-tab.active{color:#111827;border-bottom-color:var(--accent)}.auth-title{font-size:14px;color:#6b7280;margin:0 0 16px}
.auth-google{display:flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:9px;background:#fff;color:#111827;text-decoration:none;font-weight:650;font-size:14px}
.auth-divider{display:flex;align-items:center;gap:10px;margin:18px 0;color:#9ca3af;font-size:12px}.auth-divider:before,.auth-divider:after{content:"";height:1px;background:#e5e7eb;flex:1}
.auth-field{width:100%;padding:11px 12px;border:1px solid #d1d5db;border-radius:9px;font:inherit;font-size:14px;margin-bottom:12px}.auth-field:focus{outline:2px solid color-mix(in srgb,var(--accent) 22%,white);border-color:var(--accent)}
.auth-submit{width:100%;padding:11px 14px;border:0;border-radius:9px;background:var(--accent);color:#fff;font-weight:700;cursor:pointer}.auth-link{border:0;background:transparent;color:var(--accent);padding:0;font:inherit;font-size:13px;cursor:pointer;text-decoration:none}
.auth-forgot{display:block;margin:-3px 0 15px;text-align:right}.auth-msg{min-height:18px;margin:10px 0 0;font-size:13px;color:#b42318}.auth-msg.ok{color:#15803d}.auth-help{font-size:12px;line-height:1.5;color:#6b7280;margin:12px 0 0}
"""

_GOOGLE_ICON = """<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62z" fill="#4285F4"/><path d="M9 18c2.43 0 4.47-.81 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.34 0-4.33-1.58-5.04-3.71H.96v2.33A9 9 0 0 0 9 18z" fill="#34A853"/><path d="M3.96 10.71A5.4 5.4 0 0 1 3.68 9c0-.59.1-1.17.28-1.71V4.96H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.04l3-2.33z" fill="#FBBC05"/><path d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.58-2.58A8.64 8.64 0 0 0 9 0 9 9 0 0 0 .96 4.96l3 2.33C4.67 5.16 6.66 3.58 9 3.58z" fill="#EA4335"/></svg>"""

AUTH_JS = """
function authOpen(tab='login'){document.getElementById('auth-overlay').classList.add('visible');authTab(tab)}
function authClose(){document.getElementById('auth-overlay').classList.remove('visible')}
function authTab(tab){
  ['login','register','forgot'].forEach(x=>{document.getElementById('auth-'+x).hidden=x!==tab});
  document.querySelectorAll('.auth-tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab));
}
function authMessage(id,text,ok=false){const el=document.getElementById(id);el.textContent=text||'';el.classList.toggle('ok',ok)}
async function authPost(path, formId, msgId){
  authMessage(msgId,'');
  const response=await fetch(path,{method:'POST',body:new FormData(document.getElementById(formId)),headers:{'Accept':'application/json'}});
  let data={};try{data=await response.json()}catch(e){}
  authMessage(msgId,data.message||data.error||(response.ok?'Done':'Request failed'),response.ok);
  if(response.ok&&data.redirect)setTimeout(()=>location.assign(data.redirect),250);
  return response.ok;
}
document.addEventListener('keydown',e=>{if(e.key==='Escape')authClose()});
"""


def auth_modal(app_name: str):
    return Div(
        Div(
            Button("×", type="button", aria_label="Close sign in", cls="auth-close", onclick="authClose()"),
            Div(
                Button("Sign In", type="button", data_tab="login", cls="auth-tab active", onclick="authTab('login')"),
                Button("Register", type="button", data_tab="register", cls="auth-tab", onclick="authTab('register')"),
                cls="auth-tabs",
            ),
            Div(
                P(f"Sign in to your {app_name} account", cls="auth-title"),
                A(NotStr(_GOOGLE_ICON), Span("Continue with Google"), href="/auth/google", cls="auth-google"),
                Div("or", cls="auth-divider"),
                Form(
                    Input(name="email", type="email", placeholder="Email", autocomplete="email", required=True, cls="auth-field"),
                    Input(name="password", type="password", placeholder="Password", autocomplete="current-password", required=True, cls="auth-field"),
                    Button("Forgot password?", type="button", cls="auth-link auth-forgot", onclick="authTab('forgot')"),
                    Button("Sign In", type="submit", cls="auth-submit"),
                    onsubmit="event.preventDefault();authPost('/auth/local/login',this.id,'auth-login-msg')",
                    id="auth-login-form",
                ),
                Div(id="auth-login-msg", cls="auth-msg", role="status"),
                id="auth-login",
            ),
            Div(
                P(f"Create your {app_name} account", cls="auth-title"),
                Form(
                    Input(name="name", placeholder="Name", autocomplete="name", required=True, cls="auth-field"),
                    Input(name="email", type="email", placeholder="Email", autocomplete="email", required=True, cls="auth-field"),
                    Input(name="password", type="password", placeholder="Password (minimum 10 characters)", autocomplete="new-password", minlength="10", required=True, cls="auth-field"),
                    Button("Register", type="submit", cls="auth-submit"),
                    onsubmit="event.preventDefault();authPost('/auth/local/register',this.id,'auth-register-msg')",
                    id="auth-register-form",
                ),
                Div(id="auth-register-msg", cls="auth-msg", role="status"),
                P("We will email you a verification link before the account can sign in.", cls="auth-help"),
                id="auth-register", hidden=True,
            ),
            Div(
                P("Reset your password", cls="auth-title"),
                Form(
                    Input(name="email", type="email", placeholder="Email", autocomplete="email", required=True, cls="auth-field"),
                    Button("Send reset link", type="submit", cls="auth-submit"),
                    onsubmit="event.preventDefault();authPost('/auth/local/forgot',this.id,'auth-forgot-msg')",
                    id="auth-forgot-form",
                ),
                Div(id="auth-forgot-msg", cls="auth-msg", role="status"),
                Button("Back to sign in", type="button", cls="auth-link", onclick="authTab('login')"),
                id="auth-forgot", hidden=True,
            ),
            cls="auth-dialog", role="dialog", aria_modal="true", aria_label=f"{app_name} account",
        ),
        id="auth-overlay", cls="auth-overlay", onclick="if(event.target===this)authClose()",
    )


class AccountStore:
    def __init__(self):
        path = Path(os.getenv("FASTSME_AUTH_DB", "data/fastsme-accounts.sqlite"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._setup()

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _setup(self):
        with self._db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
              id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL DEFAULT '',
              password_hash TEXT, is_verified INTEGER NOT NULL DEFAULT 0,
              google_linked INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_tokens (
              id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
              purpose TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, expires_at INTEGER NOT NULL,
              used_at INTEGER, created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS auth_tokens_account_purpose ON auth_tokens(account_id,purpose);
            CREATE TABLE IF NOT EXISTS auth_limits (
              subject_hash TEXT NOT NULL, action TEXT NOT NULL, window_start INTEGER NOT NULL,
              attempts INTEGER NOT NULL, PRIMARY KEY(subject_hash,action)
            );
            """)

    @staticmethod
    def _email(value):
        value = (value or "").strip().lower()
        return value if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) else ""

    @staticmethod
    def _hash_password(password):
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return "scrypt$16384$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()

    @staticmethod
    def _verify_password(password, encoded):
        try:
            _, cost, salt, expected = encoded.split("$", 3)
            actual = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(cost), r=8, p=1)
            return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected))
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _issue_token(self, db, account_id, purpose, ttl):
        now = int(time.time())
        db.execute("DELETE FROM auth_tokens WHERE account_id=? AND purpose=? AND used_at IS NULL", (account_id, purpose))
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO auth_tokens(account_id,purpose,token_hash,expires_at,created_at) VALUES(?,?,?,?,?)",
            (account_id, purpose, self._token_hash(token), now + ttl, now),
        )
        return token

    def _allowed_attempt(self, subject, action, limit, window):
        now = int(time.time())
        subject_hash = hashlib.sha256((subject or "").encode()).hexdigest()
        with self._db() as db:
            row = db.execute(
                "SELECT window_start,attempts FROM auth_limits WHERE subject_hash=? AND action=?",
                (subject_hash, action),
            ).fetchone()
            if not row or row["window_start"] <= now - window:
                db.execute(
                    "INSERT INTO auth_limits(subject_hash,action,window_start,attempts) VALUES(?,?,?,1) "
                    "ON CONFLICT(subject_hash,action) DO UPDATE SET window_start=excluded.window_start,attempts=1",
                    (subject_hash, action, now),
                )
                return True
            if row["attempts"] >= limit:
                return False
            db.execute(
                "UPDATE auth_limits SET attempts=attempts+1 WHERE subject_hash=? AND action=?",
                (subject_hash, action),
            )
            return True

    def register(self, email, password, name):
        email, name = self._email(email), (name or "").strip()[:120]
        if not email or len(password or "") < 10:
            return False, "Use a valid email and a password of at least 10 characters."
        if not self._allowed_attempt(email, "register", 5, 3600):
            return False, "Too many attempts. Please try again later."
        now = int(time.time())
        with self._db() as db:
            row = db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
            if row and row["is_verified"]:
                return True, "If this address can be registered, a verification email is on its way."
            password_hash = self._hash_password(password)
            if row:
                db.execute("UPDATE accounts SET name=?,password_hash=?,updated_at=? WHERE id=?", (name, password_hash, now, row["id"]))
                account_id = row["id"]
            else:
                cur = db.execute(
                    "INSERT INTO accounts(email,name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (email, name, password_hash, now, now),
                )
                account_id = cur.lastrowid
            token = self._issue_token(db, account_id, "verify", 24 * 3600)
        if not self._send_action(email, name, "Verify your account", "verify", token):
            return False, "Verification email could not be sent. Please try again shortly."
        return True, "Check your email to verify your account."

    def verify(self, token):
        now = int(time.time())
        with self._db() as db:
            row = db.execute(
                "SELECT t.id,t.account_id FROM auth_tokens t WHERE token_hash=? AND purpose='verify' AND used_at IS NULL AND expires_at>?",
                (self._token_hash(token), now),
            ).fetchone()
            if not row:
                return None
            db.execute("UPDATE auth_tokens SET used_at=? WHERE id=?", (now, row["id"]))
            db.execute("UPDATE accounts SET is_verified=1,updated_at=? WHERE id=?", (now, row["account_id"]))
            return dict(db.execute("SELECT * FROM accounts WHERE id=?", (row["account_id"],)).fetchone())

    def login(self, email, password):
        email = self._email(email)
        if not self._allowed_attempt(email, "login", 10, 900):
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
        if not row or not row["is_verified"] or not row["password_hash"] or not self._verify_password(password or "", row["password_hash"]):
            return None
        return dict(row)

    def forgot(self, email):
        email = self._email(email)
        if not self._allowed_attempt(email, "forgot", 5, 3600):
            return
        with self._db() as db:
            row = db.execute("SELECT * FROM accounts WHERE email=? AND is_verified=1", (email,)).fetchone()
            if not row:
                return
            token = self._issue_token(db, row["id"], "reset", 3600)
        self._send_action(email, row["name"], "Reset your password", "reset", token)

    def reset(self, token, password):
        if len(password or "") < 10:
            return False
        now = int(time.time())
        with self._db() as db:
            row = db.execute(
                "SELECT id,account_id FROM auth_tokens WHERE token_hash=? AND purpose='reset' AND used_at IS NULL AND expires_at>?",
                (self._token_hash(token), now),
            ).fetchone()
            if not row:
                return False
            db.execute("UPDATE auth_tokens SET used_at=? WHERE id=?", (now, row["id"]))
            db.execute("UPDATE accounts SET password_hash=?,updated_at=? WHERE id=?",
                       (self._hash_password(password), now, row["account_id"]))
        return True

    def link_google(self, email, name=""):
        email, name, now = self._email(email), (name or "").strip()[:120], int(time.time())
        if not email:
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone()
            if row:
                db.execute("UPDATE accounts SET google_linked=1,is_verified=1,name=CASE WHEN name='' THEN ? ELSE name END,updated_at=? WHERE id=?",
                           (name, now, row["id"]))
            else:
                db.execute("INSERT INTO accounts(email,name,is_verified,google_linked,created_at,updated_at) VALUES(?,?,1,1,?,?)",
                           (email, name, now, now))
            return dict(db.execute("SELECT * FROM accounts WHERE email=?", (email,)).fetchone())

    def _send_action(self, email, name, subject, action, token):
        base = os.getenv("FASTSME_PUBLIC_URL", "").rstrip("/")
        path = "verify" if action == "verify" else "reset"
        link = f"{base}/auth/local/{path}/{token}"
        safe_name, safe_link = html.escape(name or "there"), html.escape(link, quote=True)
        body = f"<p>Hello {safe_name},</p><p><a href=\"{safe_link}\">{html.escape(subject)}</a></p><p>This link expires automatically.</p>"
        return _send_email(email, subject, body)


def _send_email(to, subject, html_body):
    token = os.getenv("POSTMARK_API_TOKEN", "")
    sender = os.getenv("FROM_EMAIL", "")
    if not token or not sender:
        return False
    payload = json.dumps({
        "From": sender, "To": to, "Subject": subject, "HtmlBody": html_body,
        "TextBody": re.sub(r"<[^>]+>", "", html_body), "MessageStream": "outbound",
    }).encode()
    request = UrlRequest(
        "https://api.postmarkapp.com/email", data=payload, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "X-Postmark-Server-Token": token},
    )
    try:
        with urlopen(request, timeout=15) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError):
        return False


accounts = AccountStore()


def reset_page(token, error=""):
    return Html(
        Head(Title("Reset password"), Meta(name="viewport", content="width=device-width, initial-scale=1"), Style(AUTH_CSS)),
        Body(Div(
            H2("Choose a new password"), P(error, cls="auth-msg") if error else None,
            Form(Input(name="token", type="hidden", value=token),
                 Input(name="password", type="password", minlength="10", required=True, placeholder="Password (minimum 10 characters)", cls="auth-field"),
                 Button("Reset password", type="submit", cls="auth-submit"),
                 method="post", action="/auth/local/reset"),
            cls="auth-dialog"), style="min-height:100vh;display:grid;place-items:center;background:#f8fafc"),
    )


def register_fasthtml_routes(rt, *, app_name, session_key=None, success_path="/", on_login=None):
    def establish_session(sess, account):
        if on_login:
            on_login(sess, account)
        elif session_key:
            sess[session_key] = account["email"]
        else:
            raise RuntimeError("session_key or on_login is required")

    @rt("/auth/local/register", methods=["POST"])
    async def local_register(request):
        form = await request.form()
        ok, message = accounts.register(form.get("email"), form.get("password"), form.get("name"))
        return JSONResponse({"message": message}, status_code=200 if ok else 400)

    @rt("/auth/local/login", methods=["POST"])
    async def local_login(request, sess):
        form = await request.form()
        account = accounts.login(form.get("email"), form.get("password"))
        if not account:
            return JSONResponse({"error": "Invalid email, password, or unverified account."}, status_code=401)
        establish_session(sess, account)
        return JSONResponse({"message": "Signed in.", "redirect": success_path})

    @rt("/auth/local/forgot", methods=["POST"])
    async def local_forgot(request):
        form = await request.form()
        accounts.forgot(form.get("email"))
        return JSONResponse({"message": "If an account exists, a reset link has been sent."})

    @rt("/auth/local/verify/{token}", methods=["GET"])
    def local_verify(token: str, sess):
        account = accounts.verify(token)
        if not account:
            return RedirectResponse("/?auth=invalid-verification", status_code=303)
        establish_session(sess, account)
        return RedirectResponse(success_path, status_code=303)

    @rt("/auth/local/reset/{token}", methods=["GET"])
    def local_reset_page(token: str):
        return reset_page(token)

    @rt("/auth/local/reset", methods=["POST"])
    async def local_reset_submit(request):
        form = await request.form()
        if not accounts.reset(form.get("token", ""), form.get("password", "")):
            return reset_page(form.get("token", ""), "Invalid or expired link, or password too short.")
        return RedirectResponse("/?auth=password-reset", status_code=303)


def register_fastapi_routes(app, *, app_name, session_key=None, success_path="/", on_login=None):
    def establish_session(session, account):
        if on_login:
            on_login(session, account)
        elif session_key:
            session[session_key] = account["email"]
        else:
            raise RuntimeError("session_key or on_login is required")

    @app.post("/auth/local/register")
    async def local_register(request):
        form = await request.form()
        ok, message = accounts.register(form.get("email"), form.get("password"), form.get("name"))
        return JSONResponse({"message": message}, status_code=200 if ok else 400)

    @app.post("/auth/local/login")
    async def local_login(request):
        form = await request.form()
        account = accounts.login(form.get("email"), form.get("password"))
        if not account:
            return JSONResponse({"error": "Invalid email, password, or unverified account."}, status_code=401)
        establish_session(request.session, account)
        return JSONResponse({"message": "Signed in.", "redirect": success_path})

    @app.post("/auth/local/forgot")
    async def local_forgot(request):
        form = await request.form()
        accounts.forgot(form.get("email"))
        return JSONResponse({"message": "If an account exists, a reset link has been sent."})

    @app.get("/auth/local/verify/{token}")
    def local_verify(request, token: str):
        account = accounts.verify(token)
        if not account:
            return RedirectResponse("/?auth=invalid-verification", status_code=303)
        establish_session(request.session, account)
        return RedirectResponse(success_path, status_code=303)

    @app.get("/auth/local/reset/{token}")
    def local_reset_page(token: str):
        return reset_page(token)

    @app.post("/auth/local/reset")
    async def local_reset_submit(request):
        form = await request.form()
        if not accounts.reset(form.get("token", ""), form.get("password", "")):
            return reset_page(form.get("token", ""), "Invalid or expired link, or password too short.")
        return RedirectResponse("/?auth=password-reset", status_code=303)
