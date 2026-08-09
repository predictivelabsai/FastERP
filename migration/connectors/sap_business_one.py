"""SAP Business One Service Layer OData v4 connector."""

from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .base import Capability, ExtractionPage, SourceRecord, canonical_hash


DEFAULT_KEYS = {
    "BusinessPartners": "CardCode",
    "Items": "ItemCode",
    "Warehouses": "WarehouseCode",
    "ChartOfAccounts": "Code",
    "Currencies": "Code",
    "SalesTaxCodes": "Code",
    "Projects": "Code",
    "Orders": "DocEntry",
    "DeliveryNotes": "DocEntry",
    "Invoices": "DocEntry",
    "IncomingPayments": "DocEntry",
    "PurchaseOrders": "DocEntry",
    "PurchaseDeliveryNotes": "DocEntry",
    "PurchaseInvoices": "DocEntry",
    "OutgoingPayments": "DocEntry",
    "JournalEntries": "JdtNum",
    "Quotations": "DocEntry",
    "Returns": "DocEntry",
    "CreditNotes": "DocEntry",
    "PurchaseReturns": "DocEntry",
    "PurchaseCreditNotes": "DocEntry",
    "ProfitCenters": "CenterCode",
    "PaymentTermsTypes": "GroupNumber",
    "UnitOfMeasurements": "AbsEntry",
    "PriceLists": "PriceListNo",
    "CurrencyRates": ("Currency", "Date"),
    "PurchaseRequests": "DocEntry",
    "PurchaseQuotations": "DocEntry",
}


class SapBusinessOneConnector:
    connector_type = "sap_business_one_odata_v4"

    def __init__(
        self,
        *,
        base_url: str,
        company_db: str,
        username: str,
        password: str,
        verify_tls: bool = True,
        timeout: float = 30,
        max_retries: int = 4,
        key_fields: dict[str, str | tuple[str, ...]] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("SAP base_url must be an absolute HTTP(S) URL")
        if not base_url.rstrip("/").endswith("/b1s/v2"):
            raise ValueError("SAP Business One OData v4 base_url must end in /b1s/v2")
        if not company_db or not username or not password:
            raise ValueError("SAP CompanyDB, username, and password are required")
        self.base_url = base_url.rstrip("/") + "/"
        self.company_db = company_db
        self.username = username
        self._password = password
        self.max_retries = max_retries
        self.key_fields = {**DEFAULT_KEYS, **(key_fields or {})}
        self._owns_client = client is None
        self.client = client or httpx.Client(
            timeout=timeout,
            verify=verify_tls,
            headers={"Accept": "application/json", "OData-Version": "4.0"},
        )
        self._logged_in = False

    @classmethod
    def from_environment(
        cls,
        *,
        base_url: str,
        company_db: str,
        credential_env_prefix: str,
        **options: Any,
    ) -> "SapBusinessOneConnector":
        prefix = credential_env_prefix.rstrip("_")
        return cls(
            base_url=base_url,
            company_db=company_db,
            username=os.getenv(f"{prefix}_USERNAME", ""),
            password=os.getenv(f"{prefix}_PASSWORD", ""),
            **options,
        )

    def login(self) -> None:
        if self._logged_in:
            return
        response = self._request(
            "POST",
            "Login",
            json={
                "CompanyDB": self.company_db,
                "UserName": self.username,
                "Password": self._password,
            },
            authenticated=False,
        )
        response.raise_for_status()
        self._logged_in = True

    def logout(self) -> None:
        if not self._logged_in:
            return
        try:
            self._request("POST", "Logout", authenticated=True).raise_for_status()
        finally:
            self._logged_in = False
            self.client.cookies.clear()

    def test_connection(self) -> dict[str, Any]:
        self.login()
        response = self._request("GET", "$metadata")
        response.raise_for_status()
        return {
            "ok": True,
            "connector_type": self.connector_type,
            "company_db": self.company_db,
            "metadata_hash": canonical_hash(response.text),
        }

    def discover(self) -> list[Capability]:
        self.login()
        response = self._request("GET", "$metadata")
        response.raise_for_status()
        root = ET.fromstring(response.text)
        entity_sets = []
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "EntitySet":
                name = element.attrib.get("Name")
                if name:
                    entity_sets.append(name)
        metadata_hash = canonical_hash(response.text)
        return [
            Capability(
                source_object=name,
                available=True,
                supports_filter=True,
                supports_expand=True,
                key_fields=(
                    (self.key_fields[name],)
                    if isinstance(self.key_fields.get(name), str)
                    else tuple(self.key_fields.get(name, ()))
                ),
                metadata_hash=metadata_hash,
            )
            for name in sorted(set(entity_sets))
        ]

    def count(self, source_object: str, *, filter_expression: str | None = None) -> int:
        self.login()
        params = {"$filter": filter_expression} if filter_expression else None
        response = self._request("GET", f"{source_object}/$count", params=params)
        response.raise_for_status()
        return int(response.text.strip())

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
        if page_size < 1 or page_size > 1000:
            raise ValueError("page_size must be between 1 and 1000")
        self.login()
        if cursor:
            url = self._validated_next_link(cursor)
            params = None
        else:
            url = source_object
            params = {"$top": page_size}
            if select:
                params["$select"] = ",".join(select)
            if filter_expression:
                params["$filter"] = filter_expression
            if expand:
                params["$expand"] = ",".join(expand)
        response = self._request("GET", url, params=params)
        response.raise_for_status()
        body = response.json()
        values = body.get("value", [])
        if not isinstance(values, list):
            raise ValueError("SAP response does not contain an OData value array")
        configured_key = self.key_fields.get(source_object)
        if not configured_key:
            raise ValueError(f"No stable key configured for SAP object {source_object}")
        key_fields = (configured_key,) if isinstance(configured_key, str) else configured_key
        records = []
        for payload in values:
            missing_keys = [field for field in key_fields if field not in payload]
            if missing_keys:
                raise ValueError(
                    f"SAP {source_object} payload is missing {', '.join(missing_keys)}"
                )
            updated = _parse_sap_datetime(
                payload.get("UpdateDate"), payload.get("UpdateTime")
            )
            records.append(
                SourceRecord(
                    source_object=source_object,
                    source_key="|".join(str(payload[field]) for field in key_fields),
                    document_number=(
                        str(payload["DocNum"]) if payload.get("DocNum") is not None else None
                    ),
                    updated_at=updated,
                    payload=payload,
                    dependencies=_sap_dependencies(source_object, payload),
                )
            )
        next_link = body.get("@odata.nextLink") or body.get("odata.nextLink")
        return ExtractionPage(tuple(records), next_link)

    def _validated_next_link(self, link: str) -> str:
        absolute = urljoin(self.base_url, link)
        expected = urlparse(self.base_url)
        parsed = urlparse(absolute)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            raise ValueError("OData nextLink changed host")
        if not parsed.path.startswith(expected.path):
            raise ValueError("OData nextLink left the configured Service Layer path")
        return absolute

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        authenticated: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        if authenticated and not self._logged_in:
            self.login()
        url = (
            path_or_url
            if path_or_url.startswith(("http://", "https://"))
            else urljoin(self.base_url, path_or_url)
        )
        response = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.client.request(method, url, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout):
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(2**attempt, 8))
                continue
            if response.status_code not in {429, 500, 502, 503, 504}:
                return response
            if attempt >= self.max_retries:
                return response
            time.sleep(_retry_delay(response, attempt))
        assert response is not None
        return response

    def close(self) -> None:
        try:
            self.logout()
        finally:
            if self._owns_client:
                self.client.close()

    def __enter__(self) -> "SapBusinessOneConnector":
        self.login()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _parse_sap_datetime(date_value: Any, time_value: Any) -> datetime | None:
    if not date_value:
        return None
    text = str(date_value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if time_value is not None and parsed.hour == parsed.minute == parsed.second == 0:
        digits = str(time_value).zfill(4)
        if digits.isdigit():
            parsed = parsed.replace(hour=int(digits[:-2]), minute=int(digits[-2:]))
    return parsed


def _sap_dependencies(source_object: str, payload: dict) -> tuple[tuple[str, str], ...]:
    default_base = {
        "DeliveryNotes": "Orders", "Invoices": "Orders",
        "PurchaseDeliveryNotes": "PurchaseOrders",
        "PurchaseInvoices": "PurchaseOrders",
        "Returns": "DeliveryNotes", "CreditNotes": "Invoices",
        "PurchaseReturns": "PurchaseDeliveryNotes",
        "PurchaseCreditNotes": "PurchaseInvoices",
        "PurchaseQuotations": "PurchaseRequests",
    }.get(source_object)
    base_types = {
        "13": "Invoices", "15": "DeliveryNotes", "17": "Orders",
        "18": "PurchaseInvoices", "20": "PurchaseDeliveryNotes",
        "22": "PurchaseOrders",
    }
    dependencies = set()
    if default_base:
        for line in payload.get("DocumentLines") or []:
            if line.get("BaseEntry") is not None:
                base_object = base_types.get(str(line.get("BaseType")), default_base)
                dependencies.add((base_object, str(line["BaseEntry"])))
    if source_object == "IncomingPayments":
        dependencies.update(
            ("Invoices", str(row["DocEntry"]))
            for row in payload.get("PaymentInvoices") or [] if row.get("DocEntry") is not None
        )
    if source_object == "OutgoingPayments":
        dependencies.update(
            ("PurchaseInvoices", str(row["DocEntry"]))
            for row in payload.get("PaymentInvoices") or [] if row.get("DocEntry") is not None
        )
    return tuple(sorted(dependencies))


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return min(max(float(value), 0), 30)
        except ValueError:
            try:
                return min(max((parsedate_to_datetime(value) - datetime.now().astimezone()).total_seconds(), 0), 30)
            except (TypeError, ValueError):
                pass
    return min(2**attempt, 8)
