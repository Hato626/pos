"""Operator-only restaurant administration, accounting, settings and audit APIs."""
import csv
import io
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal
from email_validator import validate_email, EmailNotValidError
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import func, text, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select
from . import models, adyen_service
from .db import get_session
from .security import get_current_platform_operator, get_password_hash
from .platform_models import (PlatformConfig, PlatformAdyenConfig, PlatformRestaurant, PlatformLedger,
    PlatformAdyenPayment, PlatformAdyenRefund, PlatformAuditLog)
from .platform_schemas import (ConfigUpdate, AdyenConfigUpdate, RestaurantUpdate, RestaurantCreate,
    OwnerPasswordUpdate, FeeUpdate)
from .platform_finance import config, restaurant, audit, SUPPORTED_CURRENCIES
from .platform_routes import _tenant_detail

router = APIRouter()
DB = Annotated[Session, Depends(get_session)]
Operator = Annotated[models.User, Depends(get_current_platform_operator)]


def tenant_or_404(session: Session, tenant_id: int) -> models.Tenant:
    tenant = session.get(models.Tenant, tenant_id)
    if not tenant:
        raise HTTPException(404, 'Restaurant not found')
    return tenant


def restaurant_payload(session: Session, tenant: models.Tenant) -> dict:
    r, cfg = restaurant(session, tenant.id), config(session)
    owner = session.exec(select(models.User).where(models.User.tenant_id == tenant.id,
        models.User.role == models.UserRole.owner).order_by(models.User.id)).first()
    return {**r.model_dump(), 'id':tenant.id, 'name':tenant.name, 'email':tenant.email or '',
        'phone':tenant.phone or '', 'address':tenant.address or '', 'city':tenant.city or '',
        'public_slug':tenant.public_slug, 'currency':tenant.currency_code or 'EUR',
        'created_at':tenant.created_at, 'owner_email':owner.email if owner else '',
        'owner_name':owner.full_name if owner else '',
        'effective_commission_bps':r.commission_bps if r.commission_bps is not None else cfg.default_commission_bps}


@router.get('/restaurants')
def restaurants(session: DB, actor: Operator, search: str = Query('',max_length=120),
        status: Literal['all','active','suspended']='all', offset: int=Query(0,ge=0), limit: int=Query(20,ge=1,le=100)):
    query = select(models.Tenant).outerjoin(PlatformRestaurant, PlatformRestaurant.tenant_id==models.Tenant.id)
    if search:
        pattern='%'+search.replace('%','\\%').replace('_','\\_')+'%'
        query=query.where(or_(models.Tenant.name.ilike(pattern),models.Tenant.email.ilike(pattern)))
    if status != 'all':
        query=query.where(func.coalesce(PlatformRestaurant.status,'active')==status)
    total=session.exec(select(func.count()).select_from(query.subquery())).one()
    rows=session.exec(query.order_by(models.Tenant.id.desc()).offset(offset).limit(limit)).all()
    return {'total':total,'items':[restaurant_payload(session,t) for t in rows]}


@router.get('/restaurants/{tenant_id}')
def restaurant_detail(tenant_id: int, session: DB, actor: Operator):
    tenant=tenant_or_404(session,tenant_id)
    return {**restaurant_payload(session,tenant),'details':_tenant_detail(session,tenant).model_dump()}


@router.post('/restaurants',status_code=201)
def create_restaurant(body: RestaurantCreate, session: DB, actor: Operator):
    try:
        email=validate_email(body.owner_email,check_deliverability=False).normalized
    except EmailNotValidError:
        raise HTTPException(422,'Owner email is invalid') from None
    if len(body.owner_password.encode())>72:
        raise HTTPException(422,'Password exceeds 72 UTF-8 bytes')
    if session.exec(select(models.User).where(func.lower(models.User.email)==email.lower())).first():
        raise HTTPException(409,'An account with this email already exists')
    tenant=models.Tenant(name=body.name,email=email,currency_code=body.currency,default_language='zh-CN',
        public_slug='restaurant-'+uuid.uuid4().hex[:10],qr_auto_open=True)
    session.add(tenant);session.flush()
    session.add(models.User(email=email,full_name=body.owner_name,hashed_password=get_password_hash(body.owner_password),
        tenant_id=tenant.id,role=models.UserRole.owner))
    session.add(PlatformRestaurant(tenant_id=tenant.id,commission_bps=body.commission_bps))
    audit(session,actor,'restaurant.create',str(tenant.id),{'name':tenant.name,'currency':body.currency,'commission_bps':body.commission_bps})
    try: session.commit()
    except IntegrityError:
        session.rollback();raise HTTPException(409,'Restaurant or owner already exists') from None
    return restaurant_payload(session,tenant)


@router.put('/restaurants/{tenant_id}')
def update_restaurant(tenant_id: int, body: RestaurantUpdate, session: DB, actor: Operator):
    tenant=tenant_or_404(session,tenant_id)
    r=restaurant(session,tenant_id)
    before=restaurant_payload(session,tenant)
    for field in ('name','email','phone','address','city'):
        setattr(tenant,field,getattr(body,field))
    if body.email:
        try: tenant.email=validate_email(body.email,check_deliverability=False).normalized
        except EmailNotValidError: raise HTTPException(422,'Restaurant email is invalid') from None
    for field,value in body.model_dump().items():
        if field not in ('name','email','phone','address','city'): setattr(r,field,value)
    r.updated_at=datetime.now(timezone.utc)
    session.add(r);session.add(tenant)
    if before['status']!=r.status:
        for user in session.exec(select(models.User).where(models.User.tenant_id==tenant_id)).all():
            user.token_version+=1;session.add(user)
    changes={k:{'before':before.get(k),'after':v} for k,v in body.model_dump().items() if before.get(k)!=v}
    audit(session,actor,'restaurant.update',str(tenant_id),changes)
    try: session.commit()
    except IntegrityError:
        session.rollback();raise HTTPException(409,'This Adyen balance account is already bound to another restaurant') from None
    return restaurant_payload(session,tenant)


@router.post('/restaurants/{tenant_id}/owner-password')
def reset_owner_password(tenant_id: int, body: OwnerPasswordUpdate, session: DB, actor: Operator):
    tenant_or_404(session,tenant_id)
    if len(body.password.encode())>72: raise HTTPException(422,'Password exceeds 72 UTF-8 bytes')
    owner=session.exec(select(models.User).where(models.User.tenant_id==tenant_id,
        models.User.role==models.UserRole.owner).order_by(models.User.id)).first()
    if not owner: raise HTTPException(404,'Restaurant owner not found')
    owner.hashed_password=get_password_hash(body.password);owner.token_version+=1;session.add(owner)
    audit(session,actor,'restaurant.reset_owner_password',str(tenant_id),{'user_id':owner.id})
    session.commit();return {'ok':True}


def adyen_payload(c: PlatformAdyenConfig) -> dict:
    return {field:getattr(c,field) for field in ('environment','merchant_account','balance_platform','liable_balance_account','live_prefix')} | {
        'api_key_configured':bool(c.api_key_encrypted),'balance_api_key_configured':bool(c.balance_api_key_encrypted),
        'hmac_key_configured':bool(c.hmac_key_encrypted),'ready':adyen_service.configured(c)}


@router.get('/settings')
def get_settings(session: DB, actor: Operator):
    cfg=config(session)
    return {**cfg.model_dump(),'secrets_storage_ready':bool(os.environ.get('PLATFORM_SECRETS_KEY')),
        'allow_test_payments':os.environ.get('PLATFORM_ALLOW_TEST_PAYMENTS')=='true',
        'adyen':{env:adyen_payload(session.get(PlatformAdyenConfig,env) or PlatformAdyenConfig(environment=env)) for env in ('test','live')},
        'webhook_path':'/api/platform-payments/adyen/webhook', 'currencies':sorted(SUPPORTED_CURRENCIES)}


@router.put('/settings')
def save_settings(body: ConfigUpdate, session: DB, actor: Operator):
    cfg=config(session)
    if body.payment_enabled:
        if not adyen_service.configured(session.get(PlatformAdyenConfig,body.adyen_environment)) or not body.public_base_url:
            raise HTTPException(409,'Complete the Adyen credentials, accounts and public HTTPS URL before enabling payments')
        if body.adyen_environment=='test' and os.environ.get('PLATFORM_ALLOW_TEST_PAYMENTS')!='true':
            raise HTTPException(409,'Test payments may only be enabled on an isolated staging deployment')
    before=cfg.model_dump()
    for k,v in body.model_dump().items(): setattr(cfg,k,v)
    cfg.updated_at=datetime.now(timezone.utc);session.add(cfg)
    audit(session,actor,'settings.update','platform',{k:{'before':before.get(k),'after':v} for k,v in body.model_dump().items() if before.get(k)!=v})
    session.commit();return get_settings(session,actor)


@router.put('/adyen/{environment}')
def save_adyen(environment: Literal['test','live'], body: AdyenConfigUpdate, session: DB, actor: Operator):
    c=session.get(PlatformAdyenConfig,environment) or PlatformAdyenConfig(environment=environment)
    cfg=config(session)
    if body.merchant_account!=c.merchant_account and session.exec(select(PlatformAdyenPayment.id).where(PlatformAdyenPayment.environment==environment).limit(1)).first():
        raise HTTPException(409,'Keep the original merchant account for existing payments, refunds and notifications')
    if cfg.payment_enabled and cfg.adyen_environment==environment:
        if any(getattr(body,k)!=getattr(c,k) for k in ('merchant_account','balance_platform','liable_balance_account','live_prefix')):
            raise HTTPException(409,'Disable new payments before changing active Adyen accounts')
        if body.clear_api_key or body.clear_balance_api_key or body.clear_hmac_key:
            raise HTTPException(409,'Disable new payments before removing payment credentials')
    for field in ('merchant_account','balance_platform','liable_balance_account','live_prefix'):
        setattr(c,field,getattr(body,field))
    changed=[]
    for field in ('api_key','balance_api_key','hmac_key'):
        value=getattr(body,field)
        if getattr(body,'clear_'+field):
            setattr(c,field+'_encrypted','');changed.append(field+'.cleared')
        elif value:
            if field=='hmac_key': c.previous_hmac_key_encrypted=c.hmac_key_encrypted
            setattr(c,field+'_encrypted',adyen_service.encrypt(value));changed.append(field+'.replaced')
    c.updated_at=datetime.now(timezone.utc);session.add(c)
    audit(session,actor,'adyen.configure',environment,{'merchant_account':c.merchant_account,'changed_credentials':changed})
    session.commit();return adyen_payload(c)


@router.post('/adyen/{environment}/verify')
def verify_platform(environment: Literal['test','live'], session: DB, actor: Operator):
    c=session.get(PlatformAdyenConfig,environment)
    if not c: raise HTTPException(409,'Save Adyen configuration first')
    result=adyen_service.verify_account(c,c.liable_balance_account,recipient=False)
    audit(session,actor,'adyen.verify',environment,{'balance_account':c.liable_balance_account,'status':result['status']})
    session.commit();return result


@router.post('/restaurants/{tenant_id}/adyen/{environment}/verify')
def verify_restaurant(tenant_id: int, environment: Literal['test','live'], session: DB, actor: Operator):
    tenant_or_404(session,tenant_id);r=restaurant(session,tenant_id)
    c=session.get(PlatformAdyenConfig,environment)
    if not c: raise HTTPException(409,'Save Adyen configuration first')
    result=adyen_service.verify_account(c,getattr(r,environment+'_balance_account'),getattr(r,environment+'_account_holder'))
    audit(session,actor,'restaurant.verify_account',str(tenant_id),{'environment':environment,'status':result['status']})
    session.commit();return result


def report_filters(start: date | None, end: date | None, currency: str, tenant_id: int | None):
    end=end or datetime.now(timezone.utc).date();start=start or end-timedelta(days=29)
    if end<start or (end-start).days>366: raise HTTPException(422,'Select a date range of at most 367 days')
    if currency not in SUPPORTED_CURRENCIES: raise HTTPException(422,'Unsupported currency')
    params={'start':datetime.combine(start,time.min,timezone.utc),'end':datetime.combine(end+timedelta(days=1),time.min,timezone.utc),'currency':currency}
    where='l.occurred_at>=:start AND l.occurred_at<:end AND l.currency=:currency'
    if tenant_id is not None: where+=' AND l.tenant_id=:tenant_id';params['tenant_id']=tenant_id
    return where,params


TOTALS_SQL="""coalesce(sum(l.amount_cents),0) AS net_sales_cents,
 coalesce(sum(CASE WHEN l.amount_cents>0 AND l.kind='payment' THEN l.amount_cents ELSE 0 END),0) AS gross_sales_cents,
 coalesce(sum(CASE WHEN l.amount_cents<0 THEN -l.amount_cents ELSE 0 END),0) AS reversals_cents,
 coalesce(sum(l.commission_cents),0) AS commission_cents,
 coalesce(sum(CASE WHEN l.channel='adyen' THEN l.commission_cents ELSE 0 END),0) AS allocated_commission_cents,
 coalesce(sum(CASE WHEN l.channel<>'adyen' THEN l.commission_cents ELSE 0 END),0) AS receivable_commission_cents,
 coalesce(sum(l.amount_cents-l.commission_cents),0) AS restaurant_share_cents,
 coalesce(sum(coalesce(l.actual_fee_cents,l.estimated_fee_cents)),0) AS cost_cents,
 coalesce(sum(l.commission_cents-coalesce(l.actual_fee_cents,l.estimated_fee_cents)),0) AS estimated_profit_cents,
 count(*) FILTER (WHERE l.actual_fee_cents IS NULL) AS unconfirmed_cost_count,
 count(DISTINCT l.order_id) FILTER (WHERE l.kind='payment') AS paid_order_count"""


@router.get('/finance')
def finance(session: DB, actor: Operator, start: date | None=None, end: date | None=None,
        currency: str='CNY', tenant_id: int | None=None):
    where,params=report_filters(start,end,currency,tenant_id)
    totals=dict(session.execute(text('SELECT '+TOTALS_SQL+' FROM platform_ledger l WHERE '+where),params).mappings().one())
    daily=[dict(r) for r in session.execute(text("SELECT (l.occurred_at AT TIME ZONE 'UTC')::date AS day,"+TOTALS_SQL+
        ' FROM platform_ledger l WHERE '+where+' GROUP BY day ORDER BY day'),params).mappings()]
    by_restaurant=[dict(r) for r in session.execute(text('SELECT l.tenant_id,t.name,'+TOTALS_SQL+
        ' FROM platform_ledger l JOIN tenant t ON t.id=l.tenant_id WHERE '+where+' GROUP BY l.tenant_id,t.name ORDER BY commission_cents DESC'),params).mappings()]
    return {'currency':currency,'timezone':'UTC','totals':totals,'daily':daily,'restaurants':by_restaurant,
        'restaurant_count':session.exec(select(func.count()).select_from(models.Tenant)).one(),
        'suspended_count':session.exec(select(func.count()).select_from(PlatformRestaurant).where(PlatformRestaurant.status=='suspended')).one(),
        'review_count':session.exec(select(func.count()).select_from(PlatformAdyenPayment).where(PlatformAdyenPayment.state=='needs_review')).one()}


@router.get('/ledger')
def ledger(session: DB, actor: Operator, start: date | None=None, end: date | None=None,currency: str='CNY',
        tenant_id: int | None=None, offset: int=Query(0,ge=0), limit: int=Query(50,ge=1,le=200)):
    where,params=report_filters(start,end,currency,tenant_id)
    total=session.execute(text('SELECT count(*) FROM platform_ledger l WHERE '+where),params).scalar_one()
    rows=session.execute(text('SELECT l.*,t.name AS restaurant_name FROM platform_ledger l JOIN tenant t ON t.id=l.tenant_id WHERE '+where+
        ' ORDER BY l.occurred_at DESC,l.id DESC LIMIT :limit OFFSET :offset'),params|{'limit':limit,'offset':offset}).mappings()
    return {'total':total,'items':[dict(r) for r in rows]}


@router.get('/ledger/export')
def export_ledger(session: DB, actor: Operator, start: date | None=None,end: date | None=None,currency: str='CNY',tenant_id: int | None=None):
    where,params=report_filters(start,end,currency,tenant_id)
    rows=session.execute(text('SELECT l.id,l.occurred_at,t.name,l.order_id,l.currency,l.kind,l.channel,l.amount_cents,l.commission_bps,l.commission_cents,l.actual_fee_cents,l.estimated_fee_cents FROM platform_ledger l JOIN tenant t ON t.id=l.tenant_id WHERE '+where+' ORDER BY l.id LIMIT 50001'),params).all()
    if len(rows)>50000: raise HTTPException(422,'Narrow the date range to export up to 50000 entries')
    out=io.StringIO();out.write('\ufeff');writer=csv.writer(out)
    writer.writerow(['ID','Time UTC','Restaurant','Order','Currency','Kind','Channel','Amount minor units','Commission bps','Commission minor units','Actual fee minor units','Estimated fee minor units'])
    for row in rows:
        writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r','\n')) else v for v in row])
    return Response(out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="platform-ledger.csv"'})


@router.put('/ledger/{entry_id}/fee')
def record_fee(entry_id: int, body: FeeUpdate, session: DB, actor: Operator):
    row=session.get(PlatformLedger,entry_id)
    if not row or row.channel!='adyen': raise HTTPException(404,'Adyen accounting entry not found')
    previous=row.actual_fee_cents;row.actual_fee_cents=body.actual_fee_cents;session.add(row)
    audit(session,actor,'finance.record_actual_fee',str(entry_id),{'previous':previous,'actual_fee_cents':body.actual_fee_cents,'note':body.note})
    session.commit();return {'ok':True}


@router.get('/payments')
def payments(session: DB, actor: Operator, tenant_id: int | None=None,offset: int=Query(0,ge=0),limit: int=Query(30,ge=1,le=100)):
    q=select(PlatformAdyenPayment)
    if tenant_id is not None:q=q.where(PlatformAdyenPayment.tenant_id==tenant_id)
    total=session.exec(select(func.count()).select_from(q.subquery())).one()
    rows=session.exec(q.order_by(PlatformAdyenPayment.created_at.desc()).offset(offset).limit(limit)).all()
    items=[]
    for p in rows:
        refunds=session.exec(select(PlatformAdyenRefund).where(PlatformAdyenRefund.payment_id==p.id)).all()
        items.append(p.model_dump()|{'restaurant_name':tenant_or_404(session,p.tenant_id).name,'refunds':[r.model_dump() for r in refunds]})
    return {'total':total,'items':items}


@router.get('/audit')
def audit_log(session: DB,actor: Operator,offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100)):
    total=session.exec(select(func.count()).select_from(PlatformAuditLog)).one()
    rows=session.exec(select(PlatformAuditLog).order_by(PlatformAuditLog.id.desc()).offset(offset).limit(limit)).all()
    return {'total':total,'items':[r.model_dump() for r in rows]}
