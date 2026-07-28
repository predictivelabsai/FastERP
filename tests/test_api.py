"""FastAPI contract smoke tests."""
from fastapi.testclient import TestClient

import db
import seed
from api_app import app


def test_swagger_and_report_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "api.sqlite"))
    seed.build()
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == 200
        response = client.get("/v1/reports/trial-balance")
        assert response.status_code == 200
        assert response.json()["balanced"] is True


def test_invoice_preview_is_non_posting(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "api.sqlite"))
    seed.build()
    with TestClient(app) as client:
        response = client.post("/v1/invoices", json={
            "customer_id": 1,
            "currency": "GBP",
            "lines": [{
                "item_code": "ITM-1005",
                "description": "Synthetic thermostat",
                "quantity": 2,
                "unit_price": 199.5,
                "tax_code": "UK20",
            }],
        })
    assert response.status_code == 202
    assert response.json()["posted"] is False
