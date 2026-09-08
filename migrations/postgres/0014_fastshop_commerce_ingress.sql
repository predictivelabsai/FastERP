CREATE TABLE IF NOT EXISTS fast_erp.commerce_order_ingress (
    id              BIGSERIAL PRIMARY KEY,
    company_id      BIGINT NOT NULL REFERENCES fast_erp.companies(id),
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    payload_json    JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'Received'
                    CHECK (status IN ('Received', 'Mapped', 'Posted', 'Rejected')),
    erp_order_id    BIGINT REFERENCES fast_erp.sales_orders(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, source, source_id),
    UNIQUE (company_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_commerce_ingress_status
    ON fast_erp.commerce_order_ingress (company_id, status, created_at);

INSERT INTO fast_erp.schema_migrations(version)
VALUES ('0014_fastshop_commerce_ingress')
ON CONFLICT (version) DO NOTHING;

