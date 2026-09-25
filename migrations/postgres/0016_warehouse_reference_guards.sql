-- Enforce tenant and stock identity consistency at the database boundary.

CREATE FUNCTION fast_erp.validate_wms_location() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_record RECORD;
    zone_record RECORD;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM fast_erp.warehouses
                   WHERE id=NEW.warehouse_id AND company_id=NEW.company_id) THEN
        RAISE EXCEPTION 'Location warehouse belongs to another company';
    END IF;
    IF NEW.parent_id IS NOT NULL THEN
        SELECT company_id,warehouse_id INTO parent_record
          FROM fast_erp.warehouse_locations WHERE id=NEW.parent_id;
        IF NOT FOUND OR parent_record.company_id<>NEW.company_id
           OR parent_record.warehouse_id<>NEW.warehouse_id THEN
            RAISE EXCEPTION 'Location parent must be in the same warehouse';
        END IF;
        IF EXISTS (
            WITH RECURSIVE ancestors(id,parent_id,path) AS (
                SELECT id,parent_id,ARRAY[id]
                  FROM fast_erp.warehouse_locations WHERE id=NEW.parent_id
                UNION ALL
                SELECT parent.id,parent.parent_id,ancestors.path || parent.id
                  FROM fast_erp.warehouse_locations parent
                  JOIN ancestors ON parent.id=ancestors.parent_id
                 WHERE NOT parent.id=ANY(ancestors.path)
            ) SELECT 1 FROM ancestors WHERE id=NEW.id
        ) THEN
            RAISE EXCEPTION 'Location hierarchy cannot contain a cycle';
        END IF;
    END IF;
    IF NEW.temperature_zone_id IS NOT NULL THEN
        SELECT company_id,warehouse_id INTO zone_record
          FROM fast_erp.temperature_zones WHERE id=NEW.temperature_zone_id;
        IF NOT FOUND OR zone_record.company_id<>NEW.company_id
           OR zone_record.warehouse_id<>NEW.warehouse_id THEN
            RAISE EXCEPTION 'Temperature zone must be in the same warehouse';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER wms_location_references
BEFORE INSERT OR UPDATE ON fast_erp.warehouse_locations
FOR EACH ROW EXECUTE FUNCTION fast_erp.validate_wms_location();

CREATE FUNCTION fast_erp.validate_wms_stock_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    item_company BIGINT;
    warehouse_company BIGINT;
    location_record RECORD;
    batch_record RECORD;
    serial_record RECORD;
    ledger_record RECORD;
BEGIN
    SELECT company_id INTO item_company FROM fast_erp.items WHERE id=NEW.item_id;
    SELECT company_id INTO warehouse_company FROM fast_erp.warehouses
     WHERE id=NEW.warehouse_id;
    SELECT company_id,warehouse_id INTO location_record
      FROM fast_erp.warehouse_locations WHERE id=NEW.location_id;
    IF item_company IS DISTINCT FROM NEW.company_id
       OR warehouse_company IS DISTINCT FROM NEW.company_id
       OR location_record.company_id IS DISTINCT FROM NEW.company_id
       OR location_record.warehouse_id IS DISTINCT FROM NEW.warehouse_id THEN
        RAISE EXCEPTION 'Stock item, warehouse and location must share a company';
    END IF;
    IF NEW.batch_id IS NOT NULL THEN
        SELECT company_id,item_id INTO batch_record
          FROM fast_erp.batches WHERE id=NEW.batch_id;
        IF batch_record.company_id IS DISTINCT FROM NEW.company_id
           OR batch_record.item_id IS DISTINCT FROM NEW.item_id THEN
            RAISE EXCEPTION 'Stock lot must match company and item';
        END IF;
    END IF;
    IF NEW.serial_number_id IS NOT NULL THEN
        SELECT company_id,item_id,batch_id INTO serial_record
          FROM fast_erp.serial_numbers WHERE id=NEW.serial_number_id;
        IF serial_record.company_id IS DISTINCT FROM NEW.company_id
           OR serial_record.item_id IS DISTINCT FROM NEW.item_id
           OR (serial_record.batch_id IS NOT NULL AND
               serial_record.batch_id IS DISTINCT FROM NEW.batch_id) THEN
            RAISE EXCEPTION 'Stock serial must match company, item and lot';
        END IF;
    END IF;
    IF TG_TABLE_NAME='inventory_allocation_splits' THEN
        SELECT company_id,item_id,warehouse_id INTO ledger_record
          FROM fast_erp.inventory_ledger_entries WHERE id=NEW.ledger_entry_id;
        IF ledger_record.company_id IS DISTINCT FROM NEW.company_id
           OR ledger_record.item_id IS DISTINCT FROM NEW.item_id
           OR ledger_record.warehouse_id IS DISTINCT FROM NEW.warehouse_id THEN
            RAISE EXCEPTION 'Physical split must match its inventory ledger entry';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER wms_stock_slice_references
BEFORE INSERT OR UPDATE ON fast_erp.stock_slices
FOR EACH ROW EXECUTE FUNCTION fast_erp.validate_wms_stock_identity();
CREATE TRIGGER wms_inventory_split_references
BEFORE INSERT ON fast_erp.inventory_allocation_splits
FOR EACH ROW EXECUTE FUNCTION fast_erp.validate_wms_stock_identity();

CREATE FUNCTION fast_erp.validate_wms_reservation() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    order_record RECORD;
    slice_record RECORD;
BEGIN
    SELECT ord.company_id,line.item_id,line.warehouse_id INTO order_record
      FROM fast_erp.sales_order_items line
      JOIN fast_erp.sales_orders ord ON ord.id=line.order_id
     WHERE line.id=NEW.sales_order_item_id;
    SELECT company_id,item_id,warehouse_id INTO slice_record
      FROM fast_erp.stock_slices WHERE id=NEW.stock_slice_id;
    IF order_record.company_id IS DISTINCT FROM NEW.company_id
       OR slice_record.company_id IS DISTINCT FROM NEW.company_id
       OR order_record.item_id IS DISTINCT FROM slice_record.item_id
       OR order_record.warehouse_id IS DISTINCT FROM slice_record.warehouse_id THEN
        RAISE EXCEPTION 'Reservation must match order and stock company, item and warehouse';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER wms_reservation_references
BEFORE INSERT OR UPDATE ON fast_erp.stock_reservations
FOR EACH ROW EXECUTE FUNCTION fast_erp.validate_wms_reservation();

CREATE TABLE fast_erp.warehouse_allocation_overrides (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES fast_erp.companies(id),
    sales_order_item_id BIGINT NOT NULL REFERENCES fast_erp.sales_order_items(id),
    stock_slice_id BIGINT NOT NULL REFERENCES fast_erp.stock_slices(id),
    quantity NUMERIC(20, 6) NOT NULL CHECK (quantity > 0),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TRIGGER warehouse_allocation_overrides_immutable
BEFORE UPDATE OR DELETE ON fast_erp.warehouse_allocation_overrides
FOR EACH ROW EXECUTE FUNCTION fast_erp.reject_immutable_ledger_mutation();
CREATE INDEX idx_wms_override_order ON fast_erp.warehouse_allocation_overrides
    (company_id, sales_order_item_id, created_at DESC);

INSERT INTO fast_erp.schema_migrations(version)
VALUES ('0016_warehouse_reference_guards')
ON CONFLICT (version) DO NOTHING;
