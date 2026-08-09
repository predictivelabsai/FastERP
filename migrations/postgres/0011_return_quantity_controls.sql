-- Line-level controls preventing duplicate or excessive returns and credits.

ALTER TABLE fast_erp.sales_delivery_items
    ADD COLUMN IF NOT EXISTS returned_qty NUMERIC(20, 6) NOT NULL DEFAULT 0,
    ADD CONSTRAINT sales_delivery_items_returned_qty_check
        CHECK (returned_qty >= 0 AND returned_qty <= qty);

ALTER TABLE fast_erp.purchase_receipt_items
    ADD COLUMN IF NOT EXISTS returned_qty NUMERIC(20, 6) NOT NULL DEFAULT 0,
    ADD CONSTRAINT purchase_receipt_items_returned_qty_check
        CHECK (returned_qty >= 0 AND returned_qty <= accepted_qty + rejected_qty);

ALTER TABLE fast_erp.invoice_items
    ADD COLUMN IF NOT EXISTS credited_qty NUMERIC(20, 6) NOT NULL DEFAULT 0,
    ADD CONSTRAINT invoice_items_credited_qty_check
        CHECK (credited_qty >= 0 AND credited_qty <= qty);

ALTER TABLE fast_erp.purchase_invoice_items
    ADD COLUMN IF NOT EXISTS debited_qty NUMERIC(20, 6) NOT NULL DEFAULT 0,
    ADD CONSTRAINT purchase_invoice_items_debited_qty_check
        CHECK (debited_qty >= 0 AND debited_qty <= qty);

INSERT INTO fast_erp.schema_migrations (version)
VALUES ('0011_return_quantity_controls')
ON CONFLICT (version) DO NOTHING;
