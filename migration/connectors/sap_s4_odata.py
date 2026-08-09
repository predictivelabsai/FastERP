"""Fail-closed placeholder for a future SAP S/4HANA OData connector."""

from __future__ import annotations

from typing import Any

from fasterp.errors import DomainError

from .base import Capability, ExtractionPage


class SapS4ODataConnector:
    """Expose the connector contract without pretending S/4 is supported."""

    connector_type = "sap_s4_odata"
    product_name = "SAP S/4HANA OData"

    def __init__(self, **configuration: Any) -> None:
        # Retain names only so a diagnostic cannot accidentally reveal a secret.
        self.configuration_keys = tuple(sorted(configuration))

    def _unsupported(self):
        raise DomainError(
            f"{self.product_name} is a non-operational connector stub; "
            "use sap_business_one_odata_v4 for the approved migration path"
        )

    def test_connection(self) -> dict[str, Any]:
        return self._unsupported()

    def discover(self) -> list[Capability]:
        return self._unsupported()

    def count(self, source_object: str, *, filter_expression: str | None = None) -> int:
        del source_object, filter_expression
        return self._unsupported()

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
        del source_object, cursor, page_size, select, filter_expression, expand
        return self._unsupported()

    def close(self) -> None:
        return None
