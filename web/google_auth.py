"""Minimal server-side Google OpenID Connect flow."""
from __future__ import annotations
import json, os, secrets
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "")

def enabled(): return bool(CLIENT_ID and CLIENT_SECRET)
def new_state(): return secrets.token_urlsafe(32)

def callback_uri(request):
    if REDIRECT_URI: return REDIRECT_URI
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/auth/google/callback"

def authorize_url(request, state):
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": CLIENT_ID, "redirect_uri": callback_uri(request),
        "response_type": "code", "scope": "openid email profile",
        "state": state, "access_type": "online", "prompt": "select_account"})

def _json_request(url, *, data=None, token=None):
    body = urlencode(data).encode() if data else None
    headers = {"Accept": "application/json"}
    if data: headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token: headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, data=body, headers=headers), timeout=20) as response:
        return json.loads(response.read())

def exchange(request, code):
    try:
        token = _json_request("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "redirect_uri": callback_uri(request), "grant_type": "authorization_code"})
        info = _json_request("https://openidconnect.googleapis.com/v1/userinfo",
                             token=token.get("access_token"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
    email = (info.get("email") or "").strip().lower()
    if not email or info.get("email_verified") is False: return None
    domains = {x.strip().lower() for x in os.getenv("GOOGLE_ALLOWED_DOMAINS", "").split(",") if x.strip()}
    emails = {x.strip().lower() for x in os.getenv("GOOGLE_ALLOWED_EMAILS", "").split(",") if x.strip()}
    if domains or emails:
        if email not in emails and email.rsplit("@", 1)[-1] not in domains: return None
    return {"email": email, "name": info.get("name") or email}
