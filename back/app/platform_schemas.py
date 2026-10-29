"""Validated platform write contracts; secrets are write-only."""
import re
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, field_validator


class WriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConfigUpdate(WriteModel):
    name: str = Field(min_length=1, max_length=120)
    support_email: str = Field(default="", max_length=254)
    support_phone: str = Field(default="", max_length=50)
    terms_url: str = Field(default="", max_length=500)
    public_base_url: str = Field(default="", max_length=300)
    default_commission_bps: int = Field(ge=0, le=10000, strict=True)
    estimated_fee_bps: int = Field(default=0, ge=0, le=10000, strict=True)
    estimated_fee_fixed_cents: int = Field(default=0, ge=0, le=100000, strict=True)
    payment_enabled: bool = False
    adyen_environment: Literal["test", "live"] = "test"
    registration_enabled: bool = True

    @field_validator("terms_url", "public_base_url")
    @classmethod
    def https_url(cls, value: str) -> str:
        if value:
            u = urlsplit(value)
            if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment:
                raise ValueError("A public HTTPS URL is required")
        return value.rstrip("/")


class AdyenConfigUpdate(WriteModel):
    merchant_account: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    balance_platform: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    liable_balance_account: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    live_prefix: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9-]*$")
    api_key: str | None = Field(default=None, max_length=4096)
    balance_api_key: str | None = Field(default=None, max_length=4096)
    hmac_key: str | None = Field(default=None, max_length=128)
    clear_api_key: bool = False
    clear_balance_api_key: bool = False
    clear_hmac_key: bool = False

    @field_validator("hmac_key")
    @classmethod
    def hmac_hex(cls, value):
        if value and not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise ValueError("HMAC key must contain 64 hexadecimal characters")
        return value


class RestaurantUpdate(WriteModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=50)
    address: str = Field(default="", max_length=500)
    city: str = Field(default="", max_length=120)
    status: Literal["active", "suspended"] = "active"
    commission_bps: int | None = Field(default=None, ge=0, le=10000, strict=True)
    notes: str = Field(default="", max_length=2000)
    recipient_name: str = Field(default="", max_length=160)
    bank_name: str = Field(default="", max_length=160)
    account_last4: str = Field(default="", max_length=4, pattern=r"^[0-9]{0,4}$")
    test_balance_account: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    live_balance_account: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    test_account_holder: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    live_account_holder: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    test_transfer_instrument: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")
    live_transfer_instrument: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_-]*$")


class RestaurantCreate(WriteModel):
    name: str = Field(min_length=1, max_length=160)
    owner_email: str = Field(min_length=3, max_length=254)
    owner_name: str = Field(min_length=1, max_length=120)
    owner_password: str = Field(min_length=12, max_length=72)
    currency: Literal["CNY", "EUR", "USD", "GBP", "HKD", "SGD", "AUD", "CAD", "NZD", "CHF"] = "CNY"
    commission_bps: int | None = Field(default=None, ge=0, le=10000, strict=True)


class OwnerPasswordUpdate(WriteModel):
    password: str = Field(min_length=12, max_length=72)


class PaymentLinkCreate(WriteModel):
    table_token: str = Field(min_length=20, max_length=100)


class RefundCreate(WriteModel):
    amount_cents: int = Field(ge=1, le=100000000, strict=True)
    reason: str = Field(min_length=3, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9-]+$")


class FeeUpdate(WriteModel):
    actual_fee_cents: int = Field(ge=0, le=100000000, strict=True)
    note: str = Field(min_length=3, max_length=500)
