"""Connector protocol, paging, security, and deterministic replay tests."""

from __future__ import annotations

import json

import httpx
import pytest

from migration.connectors import (
    CsvBundleConnector,
    ErpNextRestConnector,
    MockSapConnector,
    SapBusinessOneConnector,
    SapEccConnector,
    SapS4ODataConnector,
    SourceConnector,
)
from fasterp.errors import DomainError
from migration.connectors.base import canonical_hash, canonical_json


def test_canonical_payload_is_key_order_independent():
    left = {"b": [2, 1], "a": {"z": "€"}}
    right = {"a": {"z": "€"}, "b": [2, 1]}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_hash(left) == canonical_hash(right)


def test_mock_connector_pages_and_implements_protocol():
    connector = MockSapConnector(
        {"Items": [{"ItemCode": f"I-{n}", "Name": f"Item {n}"} for n in range(5)]},
        keys={"Items": "ItemCode"},
    )
    assert isinstance(connector, SourceConnector)
    assert connector.count("Items") == 5
    first = connector.extract("Items", page_size=2)
    second = connector.extract("Items", page_size=2, cursor=first.next_cursor)
    assert [row.source_key for row in first.records + second.records] == [
        "I-0", "I-1", "I-2", "I-3"
    ]
    assert second.next_cursor == "4"


@pytest.mark.parametrize("connector_class", [SapS4ODataConnector, SapEccConnector])
def test_future_sap_stubs_implement_protocol_and_fail_closed(connector_class):
    connector = connector_class(password="must-not-be-retained")
    assert isinstance(connector, SourceConnector)
    assert connector.configuration_keys == ("password",)
    assert not hasattr(connector, "password")
    with pytest.raises(DomainError, match="non-operational connector stub"):
        connector.test_connection()
    connector.close()


def test_csv_connector_discovers_and_pages(tmp_path):
    (tmp_path / "Items.csv").write_text(
        "ItemCode,Name\nI-1,One\nI-2,Two\nI-3,Three\n", encoding="utf-8"
    )
    connector = CsvBundleConnector(tmp_path, key_fields={"Items": "ItemCode"})
    capability = connector.discover()[0]
    assert capability.source_object == "Items"
    assert capability.estimated_count == 3
    first = connector.extract("Items", page_size=2)
    second = connector.extract("Items", page_size=2, cursor=first.next_cursor)
    assert [record.source_key for record in first.records + second.records] == [
        "I-1", "I-2", "I-3"
    ]
    with pytest.raises(ValueError):
        connector.extract("../secrets")


def test_sap_connector_login_discovery_paging_and_next_link_security():
    metadata = """<?xml version="1.0"?>
    <edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
      <edmx:DataServices><Schema xmlns="http://docs.oasis-open.org/odata/ns/edm">
        <EntityContainer Name="Service"><EntitySet Name="Orders" EntityType="SAP.Order" />
        <EntitySet Name="Items" EntityType="SAP.Item" />
        <EntitySet Name="CurrencyRates" EntityType="SAP.CurrencyRate" /></EntityContainer>
      </Schema></edmx:DataServices>
    </edmx:Edmx>"""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/Login"):
            body = json.loads(request.content)
            assert body == {"CompanyDB": "DEMO", "UserName": "user", "Password": "secret"}
            return httpx.Response(200, json={"SessionId": "hidden"})
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=metadata)
        if request.url.path.endswith("/Orders") and "$skiptoken" not in str(request.url):
            return httpx.Response(200, json={
                "value": [{"DocEntry": 1, "DocNum": 1001}],
                "@odata.nextLink": "/b1s/v2/Orders?$skiptoken=abc",
            })
        if request.url.path.endswith("/Orders"):
            return httpx.Response(200, json={"value": [{"DocEntry": 2, "DocNum": 1002}]})
        if request.url.path.endswith("/CurrencyRates"):
            return httpx.Response(200, json={
                "value": [{"Currency": "EUR", "Date": "2026-08-09", "Rate": 0.86}]
            })
        if request.url.path.endswith("/Logout"):
            return httpx.Response(204)
        raise AssertionError(request.url)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = SapBusinessOneConnector(
        base_url="https://sap.example/b1s/v2", company_db="DEMO",
        username="user", password="secret", client=client,
    )
    discovered = connector.discover()
    assert [cap.source_object for cap in discovered] == ["CurrencyRates", "Items", "Orders"]
    assert connector.extract("CurrencyRates").records[0].source_key == "EUR|2026-08-09"
    first = connector.extract("Orders", page_size=1)
    second = connector.extract("Orders", page_size=1, cursor=first.next_cursor)
    assert [record.source_key for record in first.records + second.records] == ["1", "2"]
    with pytest.raises(ValueError):
        connector.extract("Orders", cursor="https://attacker.example/b1s/v2/Orders")
    connector.close()
    assert sum(request.url.path.endswith("/Login") for request in requests) == 1
    client.close()


def test_erpnext_connector_uses_resources_and_stable_name_cursor():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/frappe.auth.get_logged_user"):
            return httpx.Response(200, json={"message": "migration@example.com"})
        if request.url.path.endswith("/frappe.client.get_count"):
            return httpx.Response(200, json={"message": 2})
        if request.url.path.endswith("/api/resource/Sales Order"):
            offset = int(request.url.params.get("limit_start", 0))
            data = [{"name": "SO-1", "modified": "2026-08-01T10:00:00"}] if offset == 0 else []
            return httpx.Response(200, json={"data": data})
        if request.url.path.endswith("/api/resource/Sales Order/SO-1"):
            return httpx.Response(200, json={"data": {
                "name": "SO-1", "modified": "2026-08-01T10:00:00",
                "items": [{"item_code": "I-1", "qty": 1}],
            }})
        raise AssertionError(request.url)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    connector = ErpNextRestConnector(
        base_url="https://erpnext.example", api_key="key", api_secret="secret",
        objects=("Sales Order",), client=client,
    )
    assert connector.test_connection()["ok"]
    assert connector.discover()[0].estimated_count == 2
    page = connector.extract("Sales Order", page_size=2)
    assert page.records[0].source_key == "SO-1"
    assert page.records[0].payload["items"][0]["item_code"] == "I-1"
    assert page.next_cursor is None
    client.close()
