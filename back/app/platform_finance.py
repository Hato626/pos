"""Commission snapshots, accounting queries and redacted operator audit records."""
import json
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlmodel import Session, select
from . import models
from .platform_models import PlatformConfig, PlatformRestaurant, PlatformOrderTerms, PlatformAuditLog

SUPPORTED_CURRENCIES = frozenset({'CNY','EUR','USD','GBP','HKD','SGD','AUD','CAD','NZD','CHF'})


def config(session: Session) -> PlatformConfig:
    return session.get(PlatformConfig, 1) or PlatformConfig()


def restaurant(session: Session, tenant_id: int) -> PlatformRestaurant:
    return session.get(PlatformRestaurant, tenant_id) or PlatformRestaurant(tenant_id=tenant_id)


def require_active(session: Session, tenant_id: int) -> None:
    if restaurant(session, tenant_id).status != 'active':
        raise HTTPException(403, detail='Restaurant is suspended by the platform')


def commission(amount: int, rate: int) -> int:
    return (max(0, amount) * rate + 5000) // 10000


def freeze_terms(session: Session, order: models.Order) -> PlatformOrderTerms:
    # Caller holds the order row lock. These terms survive later setting changes.
    existing = session.get(PlatformOrderTerms, order.id)
    if existing:
        return existing
    tenant = session.get(models.Tenant, order.tenant_id)
    cfg, r = config(session), restaurant(session, order.tenant_id)
    terms = PlatformOrderTerms(order_id=order.id, tenant_id=order.tenant_id,
        currency=(tenant.currency_code or 'EUR').upper(),
        commission_bps=r.commission_bps if r.commission_bps is not None else cfg.default_commission_bps,
        estimated_fee_bps=cfg.estimated_fee_bps, estimated_fee_fixed_cents=cfg.estimated_fee_fixed_cents)
    session.add(terms)
    session.flush()
    return terms


def audit(session: Session, actor: models.User, action: str, target: str, details: dict) -> None:
    # Callers supply allowlisted details; never serialize a request containing credentials.
    session.add(PlatformAuditLog(actor_id=actor.id, action=action, target=target,
        details=json.dumps(details,ensure_ascii=False,default=str)))


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
