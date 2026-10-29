CREATE TABLE IF NOT EXISTS deployment_printer_binding (
 tenant_id integer PRIMARY KEY REFERENCES tenant(id),
 agent_id integer REFERENCES print_agent(id),
 kitchen_printer text NOT NULL DEFAULT 'Deli DL-720C',
 receipt_printer text NOT NULL DEFAULT 'Deli DL-720C',
 auto_kitchen boolean NOT NULL DEFAULT false,
 auto_receipt boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS deployment_print_device (
 agent_id integer PRIMARY KEY REFERENCES print_agent(id),
 printers jsonb NOT NULL DEFAULT '[]',
 last_seen_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE deployment_print_device ADD COLUMN IF NOT EXISTS protocol_version integer NOT NULL DEFAULT 1;
CREATE TABLE IF NOT EXISTS deployment_printer_paper (
 tenant_id integer NOT NULL REFERENCES tenant(id),
 agent_id integer NOT NULL REFERENCES print_agent(id),
 printer text NOT NULL,
 width_mm integer NOT NULL,
 height_mm integer NOT NULL,
 PRIMARY KEY(tenant_id,agent_id,printer),
 CHECK ((width_mm,height_mm) IN ((50,60),(30,60),(60,50),(60,30),(60,40)))
);
CREATE TABLE IF NOT EXISTS deployment_print_event (
 id bigserial PRIMARY KEY,
 tenant_id integer NOT NULL REFERENCES tenant(id),
 order_id integer NOT NULL REFERENCES "order"(id),
 kind text NOT NULL,
 payload jsonb NOT NULL,
 job_id integer REFERENCES print_job(id),
 processed_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS deployment_print_event_pending
 ON deployment_print_event(id) WHERE processed_at IS NULL;

-- Capture only ticket fields; credentials and tenant payment keys never enter the queue.
CREATE OR REPLACE FUNCTION deployment_ticket_snapshot(oid integer) RETURNS jsonb AS $$
 SELECT jsonb_build_object(
  'order', jsonb_build_object('id',o.id,'table_name',coalesce(t.name,'自取'),
   'customer_name',o.customer_name,'notes',o.notes,'created_at',o.created_at,
   'paid_at',o.paid_at,'payment_method',o.payment_method,'status',o.status,
   'tip_amount_cents',o.tip_amount_cents,'loyalty_discount_cents',o.loyalty_discount_cents,
   'delivery_fee_cents',CASE WHEN o.order_channel::text='satisfecho_delivery' THEN o.delivery_fee_cents ELSE 0 END),
  'tenant',jsonb_build_object('name',n.name,'currency_code',n.currency_code,'timezone',n.timezone),
  'items',coalesce((SELECT jsonb_agg(jsonb_build_object('id',i.id,'name',i.product_name,
   'quantity',i.quantity,'price_cents',i.price_cents,'notes',i.notes,
   'customization_summary',i.customization_summary,'line_modifiers_summary',i.line_modifiers_summary,
   'removed_by_customer',i.removed_by_customer,'removed_by_user_id',i.removed_by_user_id,'status',i.status)
   ORDER BY i.id) FROM orderitem i WHERE i.order_id=o.id),'[]'::jsonb),
  'payments',coalesce((SELECT jsonb_agg(jsonb_build_object('amount_cents',p.amount_cents,
   'payment_method',p.payment_method)) FROM order_payment p WHERE p.order_id=o.id AND p.voided_at IS NULL),'[]'::jsonb)
 ) FROM "order" o JOIN tenant n ON n.id=o.tenant_id LEFT JOIN "table" t ON t.id=o.table_id WHERE o.id=oid
$$ LANGUAGE sql;

CREATE OR REPLACE FUNCTION deployment_capture_item_print() RETURNS trigger AS $$
DECLARE tid integer; snapshot jsonb; oldrow jsonb := NULL;
BEGIN
 SELECT tenant_id INTO tid FROM "order" WHERE id=NEW.order_id AND deleted_at IS NULL AND status::text<>'cancelled';
 IF tid IS NULL OR NOT EXISTS(SELECT 1 FROM deployment_printer_binding WHERE tenant_id=tid AND auto_kitchen) THEN RETURN NEW; END IF;
 IF TG_OP='UPDATE' THEN
  IF (NEW.product_name,NEW.quantity,NEW.notes,NEW.customization_summary,NEW.line_modifiers_summary,
      NEW.removed_by_customer,NEW.removed_by_user_id,NEW.status::text='cancelled') IS NOT DISTINCT FROM
     (OLD.product_name,OLD.quantity,OLD.notes,OLD.customization_summary,OLD.line_modifiers_summary,
      OLD.removed_by_customer,OLD.removed_by_user_id,OLD.status::text='cancelled') THEN RETURN NEW; END IF;
  oldrow := to_jsonb(OLD);
 END IF;
 snapshot := deployment_ticket_snapshot(NEW.order_id);
 INSERT INTO deployment_print_event(tenant_id,order_id,kind,payload)
 VALUES(tid,NEW.order_id,'kitchen',snapshot || jsonb_build_object('old',oldrow,'new',to_jsonb(NEW)));
 RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS deployment_item_print ON orderitem;
CREATE TRIGGER deployment_item_print AFTER INSERT OR UPDATE ON orderitem
 FOR EACH ROW EXECUTE FUNCTION deployment_capture_item_print();

CREATE OR REPLACE FUNCTION deployment_capture_order_print() RETURNS trigger AS $$
BEGIN
 IF NEW.deleted_at IS NOT NULL THEN RETURN NEW; END IF;
 IF NEW.paid_at IS NOT NULL AND (TG_OP='INSERT' OR NEW.paid_at IS DISTINCT FROM OLD.paid_at)
 AND EXISTS(SELECT 1 FROM deployment_printer_binding WHERE tenant_id=NEW.tenant_id AND auto_receipt) THEN
  INSERT INTO deployment_print_event(tenant_id,order_id,kind,payload)
  VALUES(NEW.tenant_id,NEW.id,'receipt',deployment_ticket_snapshot(NEW.id));
 END IF;
 IF TG_OP='UPDATE' AND NEW.status::text='cancelled' AND OLD.status::text<>'cancelled'
 AND EXISTS(SELECT 1 FROM deployment_printer_binding WHERE tenant_id=NEW.tenant_id AND auto_kitchen) THEN
  INSERT INTO deployment_print_event(tenant_id,order_id,kind,payload)
  VALUES(NEW.tenant_id,NEW.id,'cancel',deployment_ticket_snapshot(NEW.id));
 END IF;
 RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS deployment_order_print ON "order";
CREATE TRIGGER deployment_order_print AFTER INSERT OR UPDATE ON "order"
 FOR EACH ROW EXECUTE FUNCTION deployment_capture_order_print();
