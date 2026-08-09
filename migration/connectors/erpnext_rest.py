"""ERPNext/Frappe REST connector using public resource APIs only."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import httpx

from .base import Capability, ExtractionPage, SourceRecord, canonical_hash


DEFAULT_OBJECTS = (
    "Company", "Currency", "Account", "Cost Center", "Project", "Warehouse",
    "Sales Taxes and Charges Template", "Purchase Taxes and Charges Template",
    "Customer", "Supplier", "Item", "UOM", "Item Price", "Sales Order",
    "Quotation", "Delivery Note", "Sales Invoice", "Payment Entry", "Purchase Order",
    "Purchase Receipt", "Purchase Invoice", "Journal Entry", "Stock Entry",
    "Stock Reconciliation",
    "Currency Exchange", "Payment Terms Template",
    "Material Request", "Request for Quotation", "Supplier Quotation",
)


class ErpNextRestConnector:
    connector_type = "erpnext_rest"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_secret: str,
        objects: tuple[str, ...] = DEFAULT_OBJECTS,
        timeout: float = 30,
        verify_tls: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("ERPNext base_url must be an absolute HTTP(S) URL")
        if not api_key or not api_secret:
            raise ValueError("ERPNext API key and secret are required")
        self.base_url = base_url.rstrip("/") + "/"
        self.objects = objects
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            verify=verify_tls,
            headers={
                "Accept": "application/json",
                "Authorization": f"token {api_key}:{api_secret}",
            },
        )

    def test_connection(self) -> dict[str, Any]:
        response = self.client.get(urljoin(self.base_url, "api/method/frappe.auth.get_logged_user"))
        response.raise_for_status()
        return {"ok": True, "connector_type": self.connector_type}

    def discover(self) -> list[Capability]:
        capabilities = []
        for doctype in self.objects:
            try:
                count = self.count(doctype)
                capabilities.append(
                    Capability(
                        doctype, True, True, False, ("name",),
                        estimated_count=count,
                        metadata_hash=canonical_hash({"doctype": doctype, "count": count}),
                    )
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {403, 404}:
                    capabilities.append(Capability(doctype, False, key_fields=("name",)))
                else:
                    raise
        return capabilities

    def count(self, source_object: str, *, filter_expression: str | None = None) -> int:
        params: dict[str, Any] = {"doctype": source_object}
        if filter_expression:
            params["filters"] = filter_expression
        response = self.client.get(
            urljoin(self.base_url, "api/method/frappe.client.get_count"), params=params
        )
        response.raise_for_status()
        return int(response.json().get("message", 0))

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
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        offset = int(cursor or 0)
        fields = list(select) if select else ["*"]
        params: dict[str, Any] = {
            "fields": __import__("json").dumps(fields),
            "limit_start": offset,
            "limit_page_length": page_size,
            "order_by": "name asc",
        }
        if filter_expression:
            params["filters"] = filter_expression
        response = self.client.get(
            urljoin(self.base_url, f"api/resource/{quote(source_object, safe='')}"),
            params=params,
        )
        response.raise_for_status()
        values = response.json().get("data", [])
        records = []
        for payload in values:
            if "name" not in payload:
                raise ValueError(f"ERPNext {source_object} payload is missing name")
            if source_object in _CHILD_TABLE_OBJECTS:
                detail_response = self.client.get(
                    urljoin(
                        self.base_url,
                        f"api/resource/{quote(source_object, safe='')}/{quote(str(payload['name']), safe='')}",
                    )
                )
                detail_response.raise_for_status()
                payload = detail_response.json().get("data", payload)
            modified = payload.get("modified")
            records.append(
                SourceRecord(
                    source_object=source_object,
                    source_key=str(payload["name"]),
                    document_number=str(payload["name"]),
                    updated_at=(datetime.fromisoformat(modified) if modified else None),
                    payload=payload,
                    dependencies=_erpnext_dependencies(source_object, payload),
                )
            )
        next_cursor = str(offset + len(records)) if len(records) == page_size else None
        return ExtractionPage(tuple(records), next_cursor)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()


_CHILD_TABLE_OBJECTS = {
    "Sales Order", "Quotation", "Delivery Note", "Sales Invoice", "Payment Entry",
    "Purchase Order", "Purchase Receipt", "Purchase Invoice", "Journal Entry",
    "Stock Entry", "Stock Reconciliation",
    "Payment Terms Template",
    "Material Request", "Request for Quotation", "Supplier Quotation",
    "Sales Taxes and Charges Template", "Purchase Taxes and Charges Template",
}


def _erpnext_dependencies(source_object: str, payload: dict) -> tuple[tuple[str, str], ...]:
    dependencies = set()
    if source_object == "Item Price" and payload.get("item_code"):
        dependencies.add(("Item", str(payload["item_code"])))
    if source_object == "Request for Quotation":
        for row in payload.get("items") or []:
            if row.get("material_request"):
                dependencies.add(("Material Request", str(row["material_request"])))
    if source_object == "Supplier Quotation":
        for row in payload.get("items") or []:
            if row.get("request_for_quotation"):
                dependencies.add(("Request for Quotation", str(row["request_for_quotation"])))
    for row in payload.get("items") or []:
        for field, object_name in (
            ("sales_order", "Sales Order"), ("delivery_note", "Delivery Note"),
            ("purchase_order", "Purchase Order"), ("purchase_receipt", "Purchase Receipt"),
        ):
            if row.get(field):
                dependencies.add((object_name, str(row[field])))
    if source_object == "Payment Entry":
        for row in payload.get("references") or []:
            if row.get("reference_doctype") and row.get("reference_name"):
                dependencies.add((str(row["reference_doctype"]), str(row["reference_name"])))
    return tuple(sorted(dependencies))
