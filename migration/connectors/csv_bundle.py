"""Offline CSV bundle connector for recovery and customer-assisted imports."""

from __future__ import annotations

import csv
from pathlib import Path

from .base import Capability, ExtractionPage, SourceRecord, canonical_hash


class CsvBundleConnector:
    connector_type = "csv_bundle"

    def __init__(self, directory: str | Path, *, key_fields: dict[str, str] | None = None) -> None:
        self.directory = Path(directory).resolve()
        self.key_fields = key_fields or {}
        if not self.directory.is_dir():
            raise ValueError(f"CSV bundle directory does not exist: {self.directory}")

    def _path(self, source_object: str) -> Path:
        candidate = (self.directory / f"{source_object}.csv").resolve()
        if candidate.parent != self.directory:
            raise ValueError("Invalid CSV source object")
        return candidate

    def test_connection(self):
        return {"ok": True, "connector_type": self.connector_type, "files": len(list(self.directory.glob('*.csv')))}

    def discover(self) -> list[Capability]:
        capabilities = []
        for path in sorted(self.directory.glob("*.csv")):
            with path.open(newline="", encoding="utf-8-sig") as handle:
                reader = csv.reader(handle)
                fields = tuple(next(reader, ()))
            name = path.stem
            key = self.key_fields.get(name, fields[0] if fields else "")
            capabilities.append(
                Capability(
                    name, bool(fields), False, False, ((key,) if key else ()), fields,
                    self.count(name), canonical_hash({"name": name, "fields": fields}),
                )
            )
        return capabilities

    def count(self, source_object: str, *, filter_expression: str | None = None) -> int:
        if filter_expression:
            raise ValueError("CSV connector does not support source filters")
        with self._path(source_object).open(newline="", encoding="utf-8-sig") as handle:
            return sum(1 for _ in csv.DictReader(handle))

    def extract(
        self,
        source_object: str,
        *,
        cursor: str | None = None,
        page_size: int = 100,
        select: tuple[str, ...] = (),
        filter_expression: str | None = None,
        expand: tuple[str, ...] = (),
    ) -> ExtractionPage:
        del expand
        if filter_expression:
            raise ValueError("CSV connector does not support source filters")
        offset = int(cursor or 0)
        with self._path(source_object).open(newline="", encoding="utf-8-sig") as handle:
            all_rows = list(csv.DictReader(handle))
        key = self.key_fields.get(source_object) or (next(iter(all_rows[0])) if all_rows else None)
        page = all_rows[offset : offset + page_size]
        records = []
        for row in page:
            if not key or not row.get(key):
                raise ValueError(f"CSV {source_object} row is missing stable key {key}")
            payload = {field: row.get(field) for field in select} if select else row
            records.append(SourceRecord(source_object, row[key], payload))
        next_cursor = str(offset + len(page)) if offset + len(page) < len(all_rows) else None
        return ExtractionPage(tuple(records), next_cursor)

    def close(self) -> None:
        return None
