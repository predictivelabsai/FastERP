"""Deterministic in-memory SAP-shaped connector used by migration tests."""

from __future__ import annotations

from .base import Capability, ExtractionPage, SourceRecord, canonical_hash


class MockSapConnector:
    connector_type = "mock_sap"

    def __init__(self, fixtures: dict[str, list[dict]], *, keys: dict[str, str]) -> None:
        self.fixtures = fixtures
        self.keys = keys

    def test_connection(self):
        return {"ok": True, "connector_type": self.connector_type}

    def discover(self):
        return [
            Capability(
                name, True, True, False, (self.keys[name],),
                tuple(sorted(rows[0])) if rows else (), len(rows),
                canonical_hash({"object": name, "rows": len(rows)}),
            )
            for name, rows in sorted(self.fixtures.items())
        ]

    def count(self, source_object: str, *, filter_expression: str | None = None):
        del filter_expression
        return len(self.fixtures.get(source_object, ()))

    def extract(
        self,
        source_object: str,
        *,
        cursor: str | None = None,
        page_size: int = 100,
        select: tuple[str, ...] = (),
        filter_expression: str | None = None,
        expand: tuple[str, ...] = (),
    ):
        del filter_expression, expand
        offset = int(cursor or 0)
        rows = self.fixtures.get(source_object, [])
        page = rows[offset : offset + page_size]
        key = self.keys[source_object]
        records = tuple(
            SourceRecord(
                source_object,
                str(row[key]),
                ({field: row.get(field) for field in select} if select else dict(row)),
                str(row.get("DocNum")) if row.get("DocNum") is not None else None,
            )
            for row in page
        )
        next_cursor = str(offset + len(page)) if offset + len(page) < len(rows) else None
        return ExtractionPage(records, next_cursor)

    def close(self) -> None:
        return None
