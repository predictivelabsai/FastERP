-- Idempotent natural keys for source master-data application.

CREATE UNIQUE INDEX IF NOT EXISTS uq_price_book_entries_migration_key
    ON fast_erp.price_book_entries (
        price_book_id,
        item_id,
        COALESCE(uom_id, 0),
        minimum_qty,
        COALESCE(valid_from, DATE '0001-01-01'),
        COALESCE(valid_to, DATE '9999-12-31')
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_addresses_migration_key
    ON fast_erp.partner_addresses (
        partner_id,
        address_type,
        COALESCE(label, ''),
        COALESCE(address_1, ''),
        COALESCE(postal_code, ''),
        COALESCE(country_code, '')
    );

INSERT INTO fast_erp.schema_migrations (version)
VALUES ('0013_migration_master_idempotency')
ON CONFLICT (version) DO NOTHING;
