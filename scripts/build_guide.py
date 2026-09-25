#!/usr/bin/env python3
"""Render the FastERP user guide Markdown to PDF without pandoc.

Pages are separated by a line containing only ``---``. A page may open with a
Pandoc-style fenced div (``::: cover`` / ``::: toc`` / ``::: divider``) whose
name becomes the page's CSS class; otherwise the page is a standard topic page.
Markdown inside each page is rendered with python-markdown and the result is
paginated by WeasyPrint against the guide stylesheet.

    python scripts/build_guide.py docs/FastERP_user_guide_2026-07-28.md \
        --css docs/assets/guide.css
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import markdown as md
from weasyprint import HTML

FENCE = re.compile(r"^:::\s*([A-Za-z0-9_-]+)\s*$")


def render_page(chunk: str, converter: md.Markdown) -> str:
    lines = chunk.strip("\n").splitlines()
    css_class = "topic"
    if lines:
        opening = FENCE.match(lines[0].strip())
        if opening:
            css_class = opening.group(1)
            lines = lines[1:]
            while lines and lines[-1].strip() in ("", ":::"):
                if lines[-1].strip() == ":::":
                    lines.pop()
                    break
                lines.pop()
    converter.reset()
    body = converter.convert("\n".join(lines).strip())
    return f'<section class="page {css_class}">\n{body}\n</section>'


def build(source: Path, css: Path, output: Path) -> None:
    converter = md.Markdown(extensions=["extra", "sane_lists", "attr_list"])
    pages = re.split(r"(?m)^---\s*$", source.read_text(encoding="utf-8"))
    sections = "\n".join(
        render_page(page, converter) for page in pages if page.strip()
    )
    document = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<style>{css.read_text(encoding='utf-8')}</style></head>"
        f"<body>{sections}</body></html>"
    )
    HTML(string=document, base_url=str(source.parent)).write_pdf(str(output))
    print(f"Built {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--css", type=Path, default=Path("docs/assets/guide.css"))
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    output = args.output or args.source.with_suffix(".pdf")
    build(args.source, args.css, output)


if __name__ == "__main__":
    main()
