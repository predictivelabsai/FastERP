"""Search metadata, structured data, sitemap, and crawler policy."""
from __future__ import annotations

import json

from fasthtml.common import Link, Meta, NotStr, Script
from starlette.responses import Response

PRODUCT = 'FastERP'
BASE_URL = 'https://erp.fastsme.com'
DESCRIPTION = 'Connect sales, purchasing, inventory, invoicing, payments, projects, expenses, and the general ledger.'
KEYWORDS = ('FastERP', 'open source business operations', 'business operations software', 'SME business operations', 'Order to cash', 'Purchasing and inventory', 'Accounting and reporting', 'FastSME', 'open source business software')
FEATURES = ('Order to cash', 'Purchasing and inventory', 'Accounting and reporting')
SITEMAP_PATHS = ('/', '/developers')


def seo_meta(
    *,
    path: str = "/",
    title: str | None = None,
    description: str | None = None,
):
    canonical = BASE_URL + (path if path != "/" else "")
    page_title = title or f"{PRODUCT} · Open-source {KEYWORDS[2].title()}"
    page_description = description or DESCRIPTION
    structured = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": PRODUCT,
        "url": canonical,
        "description": page_description,
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web",
        "isAccessibleForFree": True,
        "license": "https://opensource.org/license/mit",
        "featureList": list(FEATURES),
        "publisher": {
            "@type": "Organization",
            "name": "FastSME",
            "url": "https://fastsme.com",
        },
    }
    return (
        Link(rel="canonical", href=canonical),
        Meta(name="robots", content="index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"),
        Meta(name="keywords", content=", ".join(KEYWORDS)),
        Meta(property="og:type", content="website"),
        Meta(property="og:site_name", content="FastSME"),
        Meta(property="og:title", content=page_title),
        Meta(property="og:description", content=page_description),
        Meta(property="og:url", content=canonical),
        Meta(name="twitter:card", content="summary"),
        Meta(name="twitter:title", content=page_title),
        Meta(name="twitter:description", content=page_description),
        Script(NotStr(json.dumps(structured, separators=(",", ":"))), type="application/ld+json"),
    )


async def sitemap():
    urls = "\n".join(
        f'  <url><loc>{BASE_URL}{path}</loc><changefreq>{"weekly" if path == "/" else "monthly"}</changefreq><priority>{"1.0" if path == "/" else "0.6"}</priority></url>'
        for path in SITEMAP_PATHS
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{urls}
</urlset>
"""
    return Response(xml, media_type="application/xml")


async def robots():
    body = f"""User-agent: *
Allow: /
Disallow: /admin
Disallow: /app
Disallow: /auth/
Disallow: /login
Disallow: /register
Disallow: /api/

Sitemap: {BASE_URL}/sitemap.xml
"""
    return Response(body, media_type="text/plain")


def register_seo_routes(app):
    paths = {getattr(route, "path", None) for route in app.routes}
    if "/sitemap.xml" not in paths:
        app.route("/sitemap.xml", methods=["GET"])(sitemap)
        app.routes.insert(0, app.routes.pop())
    if "/robots.txt" not in paths:
        app.route("/robots.txt", methods=["GET"])(robots)
        app.routes.insert(0, app.routes.pop())
