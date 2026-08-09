"""Contracts shared by every ERP source connector."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capability:
    source_object: str
    available: bool = True
    supports_filter: bool = False
    supports_expand: bool = False
    key_fields: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    estimated_count: int | None = None
    metadata_hash: str | None = None


@dataclass(frozen=True)
class SourceRecord:
    source_object: str
    source_key: str
    payload: dict[str, Any]
    document_number: str | None = None
    updated_at: datetime | None = None
    dependencies: tuple[tuple[str, str], ...] = ()

    @property
    def payload_hash(self) -> str:
        return canonical_hash(self.payload)


@dataclass(frozen=True)
class ExtractionPage:
    records: tuple[SourceRecord, ...] = ()
    next_cursor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def canonical_json(value: Any) -> str:
    """Return stable UTF-8 JSON for idempotency and snapshot evidence."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


@runtime_checkable
class SourceConnector(Protocol):
    connector_type: str

    def test_connection(self) -> dict[str, Any]: ...

    def discover(self) -> list[Capability]: ...

    def count(self, source_object: str, *, filter_expression: str | None = None) -> int: ...

    def extract(
        self,
        source_object: str,
        *,
        cursor: str | None = None,
        page_size: int = 100,
        select: tuple[str, ...] = (),
        filter_expression: str | None = None,
        expand: tuple[str, ...] = (),
    ) -> ExtractionPage: ...

    def close(self) -> None: ...
