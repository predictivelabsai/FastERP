"""Public FastERP product landing page."""
from urllib.parse import quote

from fasthtml.common import *

from .account_auth import AUTH_CSS, AUTH_JS, auth_modal

ACCENT = "#2563eb"
TINT = "#eff6ff"
FAVICON = "data:image/svg+xml," + quote(
    """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="#2563eb"/><path fill="white" d="M16 4 28 16 16 28 4 16Z"/><path fill="#2563eb" d="M11 10h11v4h-7v3h6v4h-6v5h-4Z"/></svg>""",
    safe="",
)

CSS = """
:root{--accent:#2563eb;--tint:#eff6ff;--ink:#111827;--muted:#667085;--line:#e7eaf0}
*{box-sizing:border-box} body{margin:0;background:#fff;color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif}
.lp-nav{height:68px;display:flex;align-items:center;justify-content:space-between;max-width:1180px;margin:auto;padding:0 24px;border-bottom:1px solid var(--line)}
.lp-brand{display:flex;align-items:center;gap:10px;font-weight:750;color:var(--ink);text-decoration:none} .lp-mark{width:30px;height:30px;border-radius:10px;background:var(--accent);display:grid;place-items:center;color:white}
.lp-nav-actions{display:flex;align-items:center;gap:18px} .lp-nav-link{color:var(--muted);text-decoration:none;font-size:14px;font-weight:650} .lp-nav-link:hover{color:var(--accent)}
.lp-signin,.lp-primary{display:inline-flex;align-items:center;justify-content:center;border-radius:999px;padding:10px 17px;text-decoration:none;font-weight:650;font-size:14px;cursor:pointer} .lp-signin{border:1px solid var(--line);color:var(--ink);background:white} .lp-primary{background:var(--accent);color:white;border:0}
.lp-hero{max-width:1180px;margin:auto;padding:104px 24px 76px} .lp-kicker{color:var(--accent);font-size:12px;font-weight:750;text-transform:uppercase;letter-spacing:.16em}
.lp-hero h1{font-size:clamp(42px,7vw,78px);line-height:1.02;letter-spacing:-.055em;max-width:920px;margin:22px 0} .lp-lede{font-size:20px;line-height:1.65;color:var(--muted);max-width:720px}
.lp-actions{display:flex;gap:12px;margin-top:32px;flex-wrap:wrap} .lp-secondary{color:var(--ink);font-weight:650;text-decoration:none;padding:10px 4px}
.lp-demo{max-width:960px;margin:0 auto 76px;padding:0 24px} .lp-demo-frame{padding:10px;background:#fff;border:1px solid var(--line);border-radius:22px;box-shadow:0 24px 70px rgba(17,24,39,.10)}
.lp-demo img{display:block;width:100%;height:auto;border-radius:14px;background:var(--tint)} .lp-demo p{margin:13px 0 2px;text-align:center;color:var(--muted);font-size:13px}
.lp-band{background:var(--tint);border-block:1px solid color-mix(in srgb,var(--accent) 15%,white)} .lp-grid{max-width:1180px;margin:auto;padding:64px 24px;display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.lp-card{background:rgba(255,255,255,.82);border:1px solid color-mix(in srgb,var(--accent) 15%,white);border-radius:20px;padding:26px} .lp-num{color:var(--accent);font-size:12px;font-weight:750} .lp-card h2{font-size:20px;margin:24px 0 8px} .lp-card p{color:var(--muted);line-height:1.6;margin:0}
.lp-developers{max-width:1180px;margin:auto;padding:72px 24px;display:grid;grid-template-columns:1fr auto;align-items:center;gap:32px} .lp-developers h2{font-size:32px;letter-spacing:-.03em;margin:8px 0 12px} .lp-developers p{color:var(--muted);line-height:1.65;max-width:680px;margin:0}
.lp-footer{max-width:1180px;margin:auto;padding:30px 24px 48px;color:var(--muted);font-size:13px;display:flex;justify-content:space-between;gap:20px}
@media(max-width:760px){.lp-nav{height:60px}.lp-nav-actions{gap:10px}.lp-nav-link{font-size:13px}.lp-hero{padding-top:72px}.lp-grid{grid-template-columns:1fr}.lp-developers{grid-template-columns:1fr}.lp-footer{flex-direction:column}}
"""

def landing_page():
    features = ['Order to cash', 'Purchasing and inventory', 'Accounting and reporting']
    return Html(
        Head(Title("FastERP · FastSME"), Meta(charset="utf-8"),
             Meta(name="viewport", content="width=device-width, initial-scale=1"),
             Meta(name="description", content="Connect sales, purchasing, inventory, invoicing, payments, projects, expenses, and the general ledger."),
             Link(rel="icon", type="image/svg+xml", href=FAVICON),
             Link(rel="preconnect", href="https://fonts.googleapis.com"),
             Link(rel="stylesheet", href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;750&display=swap"),
             Style(CSS + AUTH_CSS)),
        Body(
            Nav(A(Span("F", cls="lp-mark"), Span("FastERP"), href="/", cls="lp-brand"),
                Div(A("Developers", href="/developers", cls="lp-nav-link"),
                    Button("Sign In", type="button", onclick="authOpen('login')", cls="lp-signin"),
                    cls="lp-nav-actions"), cls="lp-nav"),
            Main(
                Section(Span("Business operations", cls="lp-kicker"), H1("One operational backbone for the whole business."),
                        P("Connect sales, purchasing, inventory, invoicing, payments, projects, expenses, and the general ledger.", cls="lp-lede"),
                        Div(Button("Sign In or Register", type="button", onclick="authOpen('login')", cls="lp-primary"),
                            A("Explore the open-source suite →", href="https://fastsme.com/products", cls="lp-secondary"),
                            cls="lp-actions"), cls="lp-hero"),
                Section(Div(Img(src="/static/product-demo.gif", alt="FastERP product tour",
                                loading="eager", width="1854", height="909"),
                            P("Product tour · see the workspace in action"),
                            cls="lp-demo-frame"), cls="lp-demo", aria_label="FastERP product tour"),
                Section(Div(*[Article(Span(f"0{i}", cls="lp-num"), H2(title),
                                      P("Everything you need for " + title.lower() + ", in one focused workspace."),
                                      cls="lp-card") for i, title in enumerate(features, 1)],
                            cls="lp-grid"), cls="lp-band"),
                Section(Div(Span("Developers", cls="lp-kicker"),
                            H2("Build on FastERP."),
                            P("Explore the public read API, typed schemas, examples, and token-gated integration writes.")),
                        A("Read the API documentation →", href="/developers", cls="lp-primary"),
                        cls="lp-developers"),
            ),
            Footer(Span("FastERP is part of the open-source FastSME suite."),
                   A("View all products", href="https://fastsme.com/products", style="color:var(--accent)"),
                   cls="lp-footer"),
            auth_modal("FastERP"),
            Script(AUTH_JS),
        ),
    )
