"""FastShop-to-FastERP ingress contract tests."""

from fastapi.testclient import TestClient

import db
import seed
from web.api import api


def test_fastshop_order_ingress_is_token_gated_and_idempotent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "commerce.sqlite"))
    monkeypatch.setenv("FASTSHOP_CONNECTOR_TOKEN", "integration-test-token")
    seed.build()
    payload = {
        "source": "fastshop",
        "source_id": "shop-order-1",
        "number": "FS-1001",
        "email": "buyer@example.com",
        "currency": "EUR",
        "total_minor": 8900,
        "payment_status": "paid",
        "shipping_address": {"country": "EE"},
        "lines": [
            {
                "sku": "HR-BLU-41",
                "description": "Harbour Runner",
                "variant": "Blue / 41",
                "quantity": 1,
                "unit_price_minor": 8900,
            }
        ],
    }
    headers = {
        "Authorization": "Bearer integration-test-token",
        "Idempotency-Key": "fastshop-event-0001",
    }
    with TestClient(api) as client:
        assert client.post("/v1/commerce/orders", json=payload).status_code == 401
        first = client.post("/v1/commerce/orders", json=payload, headers=headers)
        second = client.post("/v1/commerce/orders", json=payload, headers=headers)
    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
