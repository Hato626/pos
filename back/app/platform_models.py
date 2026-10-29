"""Platform operations and immutable payment terms. Amounts are minor units."""
from datetime import datetime, timezone
from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import SQLModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformConfig(SQLModel, table=True):
    __tablename__ = "platform_config"
    id: int = Field(default=1, primary_key=True)
    name: str = "Restaurant Platform"
    support_email: str = ""
    support_phone: str = ""
    terms_url: str = ""
    public_base_url: str = ""
    default_commission_bps: int = 0
    estimated_fee_bps: int = 0
    estimated_fee_fixed_cents: int = 0
    payment_enabled: bool = False
    adyen_environment: str = "test"
    registration_enabled: bool = True
    updated_at: datetime = Field(default_factory=utcnow)


class PlatformAdyenConfig(SQLModel, table=True):
    __tablename__ = "platform_adyen_config"
    environment: str = Field(primary_key=True)
    merchant_account: str = ""
    balance_platform: str = ""
    liable_balance_account: str = ""
    live_prefix: str = ""
    api_key_encrypted: str = ""
    balance_api_key_encrypted: str = ""
    hmac_key_encrypted: str = ""
    previous_hmac_key_encrypted: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class PlatformRestaurant(SQLModel, table=True):
    __tablename__ = "platform_restaurant"
    tenant_id: int = Field(primary_key=True, foreign_key="tenant.id")
    status: str = "active"
    commission_bps: int | None = None
    notes: str = ""
    recipient_name: str = ""
    bank_name: str = ""
    account_last4: str = ""
    test_balance_account: str = ""
    live_balance_account: str = ""
    test_account_holder: str = ""
    live_account_holder: str = ""
    test_transfer_instrument: str = ""
    live_transfer_instrument: str = ""
    updated_at: datetime = Field(default_factory=utcnow)


class PlatformOrderTerms(SQLModel, table=True):
    __tablename__ = "platform_order_terms"
    order_id: int = Field(primary_key=True, foreign_key="order.id")
    tenant_id: int = Field(foreign_key="tenant.id", index=True)
    currency: str
    commission_bps: int
    estimated_fee_bps: int = 0
    estimated_fee_fixed_cents: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class PlatformLedger(SQLModel, table=True):
    __tablename__ = "platform_ledger"
    id: int | None = Field(default=None, primary_key=True)
    event_key: str = Field(unique=True, index=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    payment_id: int | None = Field(default=None, foreign_key="order_payment.id")
    currency: str
    kind: str
    channel: str
    amount_cents: int
    commission_bps: int
    commission_cents: int
    estimated_fee_cents: int = 0
    actual_fee_cents: int | None = None
    description: str = ""
    occurred_at: datetime = Field(default_factory=utcnow, index=True)


class PlatformAdyenPayment(SQLModel, table=True):
    __tablename__ = "platform_adyen_payment"
    id: str = Field(primary_key=True)
    order_id: int = Field(foreign_key="order.id", index=True)
    tenant_id: int = Field(foreign_key="tenant.id", index=True)
    environment: str
    merchant_account: str
    currency: str
    amount_cents: int
    commission_cents: int
    commission_bps: int
    tip_cents: int = 0
    balance_account: str
    liable_balance_account: str
    state: str = "creating"
    link_id: str | None = None
    checkout_url: str | None = None
    psp_reference: str | None = Field(default=None, unique=True)
    payment_id: int | None = Field(default=None, foreign_key="order_payment.id")
    review_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    captured_at: datetime | None = None


class PlatformAdyenRefund(SQLModel, table=True):
    __tablename__ = "platform_adyen_refund"
    id: str = Field(primary_key=True)
    payment_id: str = Field(foreign_key="platform_adyen_payment.id", index=True)
    amount_cents: int
    commission_cents: int
    reason: str
    state: str = "creating"
    psp_reference: str | None = Field(default=None, unique=True)
    created_at: datetime = Field(default_factory=utcnow)


class PlatformWebhookEvent(SQLModel, table=True):
    __tablename__ = "platform_webhook_event"
    id: str = Field(primary_key=True)
    event_code: str
    psp_reference: str
    payment_id: str | None = None
    outcome: str
    received_at: datetime = Field(default_factory=utcnow)


class PlatformAuditLog(SQLModel, table=True):
    __tablename__ = "platform_audit_log"
    id: int | None = Field(default=None, primary_key=True)
    actor_id: int = Field(foreign_key="user.id")
    action: str
    target: str
    details: str = Field(default="{}", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utcnow, index=True)
