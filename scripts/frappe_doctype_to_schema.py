#!/usr/bin/env python3
"""Reusable Frappe → FastHTML migration helper.

Reads one or more Frappe **DocType** JSON files and prints a suggested SQLite
``CREATE TABLE`` plus a field-type mapping report. It is the first step when
porting a Frappe app to a FastHTML/SQLite demonstrator (as in
fasthtml-oss-migrations): it turns the source schema into something you can
hand-edit into a compact relational model.

Usage:
    python scripts/frappe_doctype_to_schema.py path/to/crm_lead.json [more.json ...]
    # or point it at a whole doctype directory tree:
    python scripts/frappe_doctype_to_schema.py /tmp/frappe-crm/crm/fcrm/doctype

This is intentionally generic and dependency-free (stdlib only) so it can be
copied between migration repos unchanged.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Frappe fieldtype -> (sqlite type, note). Layout-only types are skipped.
TYPE_MAP = {
    "Data": "TEXT", "Small Text": "TEXT", "Text": "TEXT", "Long Text": "TEXT",
    "Text Editor": "TEXT", "Code": "TEXT", "HTML Editor": "TEXT",
    "Select": "TEXT", "Link": "INTEGER", "Dynamic Link": "INTEGER",
    "Int": "INTEGER", "Check": "INTEGER", "Duration": "INTEGER",
    "Float": "REAL", "Currency": "REAL", "Percent": "REAL",
    "Date": "TEXT", "Datetime": "TEXT", "Time": "TEXT",
    "Attach": "TEXT", "Attach Image": "TEXT", "Phone": "TEXT", "Read Only": "TEXT",
}
SKIP = {"Section Break", "Column Break", "Tab Break", "HTML", "Heading",
        "Fold", "Table", "Table MultiSelect", "Button"}


def snake(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()
    return s or "doctype"


def convert(path: Path) -> str:
    doc = json.loads(path.read_text())
    name = doc.get("name") or path.stem
    table = snake(name)
    cols, notes = ["    id            INTEGER PRIMARY KEY"], []
    for f in doc.get("fields", []):
        ft = f.get("fieldtype", "")
        if ft in SKIP or not f.get("fieldname"):
            continue
        sqlite_type = TYPE_MAP.get(ft)
        if sqlite_type is None:
            notes.append(f"  ? {f['fieldname']}: unmapped fieldtype '{ft}' — defaulting to TEXT")
            sqlite_type = "TEXT"
        col = snake(f["fieldname"])
        if col == "id":
            col = f"{table}_id"
        link = f.get("options", "") if ft in ("Link", "Dynamic Link") else ""
        suffix = f"   -- → {link}" if link else ""
        cols.append(f"    {col:<24}{sqlite_type}{suffix}")
    ddl = f"-- from {path.name}  (DocType: {name})\n"
    ddl += f"CREATE TABLE {table} (\n" + ",\n".join(cols) + "\n);\n"
    if notes:
        ddl += "-- review:\n" + "\n".join(notes) + "\n"
    return ddl


def iter_json(paths: list[str]):
    for p in paths:
        path = Path(p)
        if path.is_dir():
            yield from sorted(path.rglob("*.json"))
        elif path.suffix == ".json":
            yield path


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    files = [p for p in iter_json(argv)
             if not p.name.endswith(("_dashboard.json", ".test_records.json"))]
    if not files:
        print("No DocType JSON files found.", file=sys.stderr)
        return 1
    for path in files:
        try:
            doc = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if doc.get("doctype") != "DocType" and "fields" not in doc:
            continue
        print(convert(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
