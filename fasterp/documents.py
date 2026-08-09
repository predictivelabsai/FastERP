"""Document numbering, state checks, and audit helpers."""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from .errors import DocumentStateError


def next_code(
    connection: Connection,
    company_id: int,
    document_type: str,
    *,
    prefix: str,
    padding: int = 6,
) -> str:
    """Allocate a gap-tolerant company/document sequence inside the transaction."""

    connection.execute(
        """INSERT INTO document_sequences
               (company_id,document_type,prefix,next_number,padding)
           VALUES (%s,%s,%s,1,%s)
           ON CONFLICT (company_id,document_type) DO NOTHING""",
        (company_id, document_type, prefix, padding),
    )
    sequence = connection.execute(
        """SELECT id,prefix,next_number,padding FROM document_sequences
            WHERE company_id=%s AND document_type=%s AND active=true FOR UPDATE""",
        (company_id, document_type),
    ).fetchone()
    if not sequence:
        raise DocumentStateError(f"No active sequence for {document_type}")
    connection.execute(
        "UPDATE document_sequences SET next_number=next_number+1,updated_at=now() WHERE id=%s",
        (sequence["id"],),
    )
    return f"{sequence['prefix']}{sequence['next_number']:0{sequence['padding']}d}"


def require_state(document: dict[str, Any], *states: str) -> None:
    if document.get("document_state") not in states:
        raise DocumentStateError(
            f"{document.get('code', 'Document')} is {document.get('document_state')}; "
            f"expected {' or '.join(states)}"
        )


def audit(
    connection: Connection,
    *,
    company_id: int,
    entity_type: str,
    entity_id: int,
    event_type: str,
    actor: str,
    previous_state: str | None = None,
    next_state: str | None = None,
    revision: int = 1,
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """INSERT INTO document_audit_events (
               company_id,entity_type,entity_id,event_type,actor,previous_state,
               next_state,revision,details)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (
            company_id, entity_type, entity_id, event_type, actor,
            previous_state, next_state, revision, Jsonb(details or {}),
        ),
    )
