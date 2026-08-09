-- Track accepted and rejected purchase-return quantities independently.

ALTER TABLE fast_erp.purchase_receipt_items
    ADD COLUMN IF NOT EXISTS accepted_returned_qty NUMERIC(20, 6) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS rejected_returned_qty NUMERIC(20, 6) NOT NULL DEFAULT 0;

UPDATE fast_erp.purchase_receipt_items
   SET accepted_returned_qty = LEAST(returned_qty, accepted_qty),
       rejected_returned_qty = GREATEST(returned_qty - accepted_qty, 0)
 WHERE returned_qty > 0
   AND accepted_returned_qty = 0
   AND rejected_returned_qty = 0;

ALTER TABLE fast_erp.purchase_receipt_items
    ADD CONSTRAINT purchase_receipt_accepted_return_check
        CHECK (accepted_returned_qty >= 0 AND accepted_returned_qty <= accepted_qty),
    ADD CONSTRAINT purchase_receipt_rejected_return_check
        CHECK (rejected_returned_qty >= 0 AND rejected_returned_qty <= rejected_qty),
    ADD CONSTRAINT purchase_receipt_return_total_check
        CHECK (returned_qty = accepted_returned_qty + rejected_returned_qty);

INSERT INTO fast_erp.schema_migrations (version)
VALUES ('0012_purchase_return_breakdown')
ON CONFLICT (version) DO NOTHING;
