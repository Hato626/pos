-- Platform configuration and payment accounting. Existing payments carry no new commission.
CREATE TABLE IF NOT EXISTS platform_config (
 id integer PRIMARY KEY DEFAULT 1 CHECK (id=1), name text NOT NULL DEFAULT 'Restaurant Platform',
 support_email text NOT NULL DEFAULT '', support_phone text NOT NULL DEFAULT '', terms_url text NOT NULL DEFAULT '',
 public_base_url text NOT NULL DEFAULT '', default_commission_bps integer NOT NULL DEFAULT 0 CHECK(default_commission_bps BETWEEN 0 AND 10000),
 estimated_fee_bps integer NOT NULL DEFAULT 0 CHECK(estimated_fee_bps BETWEEN 0 AND 10000),
 estimated_fee_fixed_cents integer NOT NULL DEFAULT 0 CHECK(estimated_fee_fixed_cents>=0),
 payment_enabled boolean NOT NULL DEFAULT false, adyen_environment text NOT NULL DEFAULT 'test' CHECK(adyen_environment IN ('test','live')),
 registration_enabled boolean NOT NULL DEFAULT true, updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO platform_config(id,name,support_email,support_phone,terms_url,public_base_url,default_commission_bps,estimated_fee_bps,estimated_fee_fixed_cents,payment_enabled,adyen_environment,registration_enabled,updated_at)
 VALUES (1,'Restaurant Platform','','','','',0,0,0,false,'test',true,now()) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS platform_adyen_config (
 environment text PRIMARY KEY CHECK(environment IN ('test','live')), merchant_account text NOT NULL DEFAULT '',
 balance_platform text NOT NULL DEFAULT '', liable_balance_account text NOT NULL DEFAULT '', live_prefix text NOT NULL DEFAULT '',
 api_key_encrypted text NOT NULL DEFAULT '', balance_api_key_encrypted text NOT NULL DEFAULT '', hmac_key_encrypted text NOT NULL DEFAULT '',
 previous_hmac_key_encrypted text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform_restaurant (
 tenant_id integer PRIMARY KEY REFERENCES tenant(id), status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
 commission_bps integer CHECK(commission_bps BETWEEN 0 AND 10000), notes text NOT NULL DEFAULT '',
 recipient_name text NOT NULL DEFAULT '', bank_name text NOT NULL DEFAULT '', account_last4 text NOT NULL DEFAULT '',
 test_balance_account text NOT NULL DEFAULT '', live_balance_account text NOT NULL DEFAULT '',
 test_account_holder text NOT NULL DEFAULT '', live_account_holder text NOT NULL DEFAULT '',
 test_transfer_instrument text NOT NULL DEFAULT '', live_transfer_instrument text NOT NULL DEFAULT '',
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_test_balance_account ON platform_restaurant(test_balance_account) WHERE test_balance_account<>'';
CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_live_balance_account ON platform_restaurant(live_balance_account) WHERE live_balance_account<>'';
CREATE TABLE IF NOT EXISTS platform_order_terms (
 order_id integer PRIMARY KEY REFERENCES "order"(id), tenant_id integer NOT NULL REFERENCES tenant(id), currency text NOT NULL,
 commission_bps integer NOT NULL CHECK(commission_bps BETWEEN 0 AND 10000), estimated_fee_bps integer NOT NULL DEFAULT 0,
 estimated_fee_fixed_cents integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform_ledger (
 id serial PRIMARY KEY, event_key text NOT NULL UNIQUE, tenant_id integer NOT NULL REFERENCES tenant(id),
 order_id integer NOT NULL REFERENCES "order"(id), payment_id integer REFERENCES order_payment(id), currency text NOT NULL,
 kind text NOT NULL, channel text NOT NULL, amount_cents integer NOT NULL, commission_bps integer NOT NULL,
 commission_cents integer NOT NULL, estimated_fee_cents integer NOT NULL DEFAULT 0, actual_fee_cents integer,
 description text NOT NULL DEFAULT '', occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_platform_ledger_tenant_date ON platform_ledger(tenant_id,occurred_at);
CREATE INDEX IF NOT EXISTS ix_platform_ledger_currency_date ON platform_ledger(currency,occurred_at);
CREATE TABLE IF NOT EXISTS platform_adyen_payment (
 id text PRIMARY KEY, order_id integer NOT NULL REFERENCES "order"(id), tenant_id integer NOT NULL REFERENCES tenant(id),
 environment text NOT NULL, merchant_account text NOT NULL, currency text NOT NULL, amount_cents integer NOT NULL CHECK(amount_cents>0),
 commission_cents integer NOT NULL CHECK(commission_cents>=0 AND commission_cents<=amount_cents), commission_bps integer NOT NULL,
 tip_cents integer NOT NULL DEFAULT 0, balance_account text NOT NULL, liable_balance_account text NOT NULL,
 state text NOT NULL DEFAULT 'creating', link_id text, checkout_url text, psp_reference text UNIQUE,
 payment_id integer REFERENCES order_payment(id), review_reason text, created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL, captured_at timestamptz
);
CREATE INDEX IF NOT EXISTS ix_platform_adyen_order ON platform_adyen_payment(order_id);
CREATE TABLE IF NOT EXISTS platform_adyen_refund (
 id text PRIMARY KEY, payment_id text NOT NULL REFERENCES platform_adyen_payment(id), amount_cents integer NOT NULL CHECK(amount_cents>0),
 commission_cents integer NOT NULL CHECK(commission_cents>=0), reason text NOT NULL, state text NOT NULL DEFAULT 'creating',
 psp_reference text UNIQUE, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform_webhook_event (
 id text PRIMARY KEY, event_code text NOT NULL, psp_reference text NOT NULL, payment_id text, outcome text NOT NULL,
 received_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS platform_audit_log (
 id serial PRIMARY KEY, actor_id integer NOT NULL REFERENCES "user"(id), action text NOT NULL, target text NOT NULL,
 details text NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);

-- Startup can create the SQLModel tables before running SQL migrations.
ALTER TABLE platform_order_terms ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE platform_order_terms ALTER COLUMN estimated_fee_bps SET DEFAULT 0;
ALTER TABLE platform_order_terms ALTER COLUMN estimated_fee_fixed_cents SET DEFAULT 0;
ALTER TABLE platform_ledger ALTER COLUMN occurred_at SET DEFAULT now();
ALTER TABLE platform_ledger ALTER COLUMN estimated_fee_cents SET DEFAULT 0;
ALTER TABLE platform_ledger ALTER COLUMN description SET DEFAULT '';

-- Do not bill pre-existing business retrospectively, including when reapplying this migration.
INSERT INTO platform_order_terms(order_id,tenant_id,currency,commission_bps,created_at)
 SELECT DISTINCT ON (p.order_id) p.order_id,p.tenant_id,upper(coalesce(t.currency_code,'EUR')),0,p.paid_at
 FROM order_payment p JOIN tenant t ON t.id=p.tenant_id
 WHERE NOT EXISTS (SELECT 1 FROM platform_order_terms x WHERE x.order_id=p.order_id)
 ORDER BY p.order_id,p.paid_at ON CONFLICT DO NOTHING;
INSERT INTO platform_ledger(event_key,tenant_id,order_id,payment_id,currency,kind,channel,amount_cents,commission_bps,commission_cents,actual_fee_cents,description,occurred_at)
 SELECT 'payment:'||p.id,p.tenant_id,p.order_id,p.id,ot.currency,'payment',p.payment_method,p.amount_cents,ot.commission_bps,0,
 CASE WHEN p.payment_method='adyen' THEN NULL ELSE 0 END,'legacy',p.paid_at
 FROM order_payment p JOIN platform_order_terms ot ON ot.order_id=p.order_id
 WHERE p.voided_at IS NULL ON CONFLICT(event_key) DO NOTHING;

CREATE OR REPLACE FUNCTION platform_payment_accounting() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE terms platform_order_terms; previous platform_ledger; cfg platform_config; rate integer; cur text; comm integer; base_total bigint; old_comm bigint;
BEGIN
 IF TG_OP='INSERT' THEN
  IF NEW.payment_method='adyen' AND NOT EXISTS (
   SELECT 1 FROM platform_adyen_payment WHERE id=current_setting('app.adyen_payment_id',true)
    AND order_id=NEW.order_id AND tenant_id=NEW.tenant_id AND amount_cents=NEW.amount_cents
  ) THEN RAISE EXCEPTION 'Adyen settlement requires a verified provider notification'; END IF;
  -- One locked order freezes pricing consistently for all partial payments.
  PERFORM 1 FROM "order" WHERE id=NEW.order_id AND tenant_id=NEW.tenant_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'Payment tenant does not match order'; END IF;
  SELECT * INTO cfg FROM platform_config WHERE id=1;
  SELECT coalesce(r.commission_bps,cfg.default_commission_bps,0),upper(coalesce(t.currency_code,'EUR')) INTO rate,cur
   FROM tenant t LEFT JOIN platform_restaurant r ON r.tenant_id=t.id WHERE t.id=NEW.tenant_id;
  INSERT INTO platform_order_terms(order_id,tenant_id,currency,commission_bps,estimated_fee_bps,estimated_fee_fixed_cents)
   VALUES(NEW.order_id,NEW.tenant_id,cur,rate,coalesce(cfg.estimated_fee_bps,0),coalesce(cfg.estimated_fee_fixed_cents,0)) ON CONFLICT DO NOTHING;
  SELECT * INTO terms FROM platform_order_terms WHERE order_id=NEW.order_id;
  IF NEW.payment_method='adyen' THEN
   SELECT commission_cents INTO comm FROM platform_adyen_payment WHERE id=current_setting('app.adyen_payment_id',true);
  ELSE
   SELECT coalesce(sum(greatest(0,amount_cents-coalesce(tip_amount_cents,0))),0) INTO base_total
    FROM order_payment WHERE order_id=NEW.order_id AND voided_at IS NULL;
   SELECT coalesce(sum(commission_cents),0) INTO old_comm FROM platform_ledger
    WHERE order_id=NEW.order_id AND kind IN ('payment','void');
   comm := greatest(0,least(NEW.amount_cents,round(base_total::numeric*terms.commission_bps/10000)::integer-old_comm));
  END IF;
  INSERT INTO platform_ledger(event_key,tenant_id,order_id,payment_id,currency,kind,channel,amount_cents,commission_bps,commission_cents,estimated_fee_cents,actual_fee_cents,occurred_at)
   VALUES('payment:'||NEW.id,NEW.tenant_id,NEW.order_id,NEW.id,terms.currency,'payment',NEW.payment_method,NEW.amount_cents,terms.commission_bps,comm,
    CASE WHEN NEW.payment_method='adyen' THEN round(NEW.amount_cents::numeric*terms.estimated_fee_bps/10000)::integer+terms.estimated_fee_fixed_cents ELSE 0 END,
    CASE WHEN NEW.payment_method='adyen' THEN NULL ELSE 0 END,NEW.paid_at) ON CONFLICT(event_key) DO NOTHING;
 ELSIF OLD.voided_at IS NULL AND NEW.voided_at IS NOT NULL THEN
  IF NEW.payment_method='adyen' THEN RAISE EXCEPTION 'Adyen payments must be refunded from platform payments'; END IF;
  SELECT * INTO previous FROM platform_ledger WHERE event_key='payment:'||NEW.id;
  IF FOUND THEN
   INSERT INTO platform_ledger(event_key,tenant_id,order_id,payment_id,currency,kind,channel,amount_cents,commission_bps,commission_cents,actual_fee_cents,occurred_at)
    VALUES('void:'||NEW.id,previous.tenant_id,previous.order_id,NEW.id,previous.currency,'void',previous.channel,-previous.amount_cents,previous.commission_bps,-previous.commission_cents,0,NEW.voided_at) ON CONFLICT(event_key) DO NOTHING;
  END IF;
 END IF;
 RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS platform_payment_accounting_trigger ON order_payment;
CREATE TRIGGER platform_payment_accounting_trigger AFTER INSERT OR UPDATE OF voided_at ON order_payment
 FOR EACH ROW EXECUTE FUNCTION platform_payment_accounting();
