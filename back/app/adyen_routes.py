"""Hosted restaurant checkout, signed payment notifications and operator refunds."""
import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request, Query, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import or_, text
from sqlmodel import Session, select
from . import models, adyen_service as adyen
from .db import get_session
from .platform_models import (PlatformAdyenConfig, PlatformAdyenPayment, PlatformAdyenRefund,
    PlatformWebhookEvent, PlatformLedger)
from .platform_schemas import PaymentLinkCreate, RefundCreate
from .platform_finance import config, restaurant, require_active, freeze_terms, commission, audit, aware, SUPPORTED_CURRENCIES
from .order_payment_service import order_due_cents, amount_paid_cents, status_after_full_payment, settlement_payment_method, list_active_payments
from .security import get_current_platform_operator
from .rate_limits import limiter, rate_limit_key_payment_order

router=APIRouter()
operator_router=APIRouter()
DB=Annotated[Session,Depends(get_session)]
Operator=Annotated[models.User,Depends(get_current_platform_operator)]


def available(session: Session, tenant_id: int) -> bool:
    cfg=config(session);r=restaurant(session,tenant_id)
    return bool(cfg.payment_enabled and r.status=='active' and
        (cfg.adyen_environment=='live' or os.environ.get('PLATFORM_ALLOW_TEST_PAYMENTS')=='true') and
        getattr(r,cfg.adyen_environment+'_balance_account') and
        adyen.configured(session.get(PlatformAdyenConfig,cfg.adyen_environment)))


def guest_order(session: Session, order_id: int, table_token: str) -> models.Order:
    table=session.exec(select(models.Table).where(models.Table.token==table_token)).first()
    if not table: raise HTTPException(404,'Order not found')
    order=session.exec(select(models.Order).where(models.Order.id==order_id,models.Order.tenant_id==table.tenant_id,
        models.Order.table_id==table.id).with_for_update()).first()
    if not order or order.deleted_at: raise HTTPException(404,'Order not found')
    return order


def payment_payload(p: PlatformAdyenPayment) -> dict:
    return {'id':p.id,'state':p.state,'checkout_url':p.checkout_url,'amount_cents':p.amount_cents,
        'currency':p.currency,'expires_at':p.expires_at,'order_id':p.order_id}


@router.post('/orders/{order_id}/link')
@limiter.limit('10/minute',key_func=rate_limit_key_payment_order)
def create_link(order_id: int, body: PaymentLinkCreate, session: DB, request: Request, response: Response):
    order=guest_order(session,order_id,body.table_token)
    require_active(session,order.tenant_id)
    if not available(session,order.tenant_id): raise HTTPException(409,'Online payment is not enabled for this restaurant')
    if order.paid_at or order.status==models.OrderStatus.cancelled: raise HTTPException(409,'Order is already settled or cancelled')
    cfg=config(session);c=session.get(PlatformAdyenConfig,cfg.adyen_environment);r=restaurant(session,order.tenant_id)
    due=order_due_cents(session,order);already=amount_paid_cents(session,order.id);remaining=due-already
    if remaining<=0:raise HTTPException(409,'Order has no remaining balance')
    now=datetime.now(timezone.utc)
    p=session.exec(select(PlatformAdyenPayment).where(PlatformAdyenPayment.order_id==order.id,
        PlatformAdyenPayment.state.in_(['creating','pending','authorised'])).order_by(PlatformAdyenPayment.created_at.desc())).first()
    if p and p.state!='authorised' and aware(p.expires_at)<=now:
        p.state='expired';session.add(p);session.flush();p=None
    if p:
        if p.amount_cents!=remaining or p.environment!=cfg.adyen_environment:
            raise HTTPException(409,'An existing checkout is pending; wait for its result or expiry before changing payment')
        if p.checkout_url:return payment_payload(p)
    else:
        adyen.verify_account(c,getattr(r,c.environment+'_balance_account'),getattr(r,c.environment+'_account_holder'))
        adyen.verify_account(c,c.liable_balance_account,recipient=False)
        terms=freeze_terms(session,order)
        if terms.currency not in SUPPORTED_CURRENCIES: raise HTTPException(409,'Order currency is not supported by this integration')
        if not cfg.public_base_url:raise HTTPException(409,'Platform public HTTPS URL is missing')
        # Tips are not commissionable. Only the remaining, unpaid tip can be excluded.
        tip_paid=sum(int(x.tip_amount_cents or 0) for x in list_active_payments(session,order.id))
        tip=min(remaining,max(0,int(order.tip_amount_cents or 0)-tip_paid))
        prior_base=sum(max(0,x.amount_cents-int(x.tip_amount_cents or 0)) for x in list_active_payments(session,order.id))
        prior_commission=session.execute(text("SELECT coalesce(sum(commission_cents),0) FROM platform_ledger WHERE order_id=:id AND kind IN ('payment','void')"),{'id':order.id}).scalar_one()
        p=PlatformAdyenPayment(id=uuid.uuid4().hex,order_id=order.id,tenant_id=order.tenant_id,environment=c.environment,
            merchant_account=c.merchant_account,currency=terms.currency,amount_cents=remaining,
            commission_cents=max(0,min(remaining,commission(prior_base+remaining-tip,terms.commission_bps)-prior_commission)),commission_bps=terms.commission_bps,
            tip_cents=tip,balance_account=getattr(r,c.environment+'_balance_account'),liable_balance_account=c.liable_balance_account,
            expires_at=now+timedelta(minutes=15))
        session.add(p)
    session.commit()  # Persist the idempotency reference before contacting the provider.
    result=adyen.api(c,'POST','/paymentLinks',key=p.id,body={
        'merchantAccount':p.merchant_account,'reference':p.id,'amount':{'currency':p.currency,'value':p.amount_cents},
        'description':f'Restaurant order {order.id}','reusable':False,'shopperLocale':'zh-CN','captureDelayHours':0,
        'expiresAt':aware(p.expires_at).isoformat(),
        'returnUrl':cfg.public_base_url+'/payment-result/'+p.id+'?table_token='+body.table_token,
        'splits':adyen.splits(p)})
    if not result.get('id') or not adyen.valid_checkout_url(result.get('url','')):
        raise HTTPException(502,'Adyen did not return a valid hosted checkout URL')
    p=session.exec(select(PlatformAdyenPayment).where(PlatformAdyenPayment.id==p.id).with_for_update()).one()
    p.link_id=result['id'];p.checkout_url=result['url']
    if p.state=='creating':p.state='pending'
    session.add(p);session.commit();return payment_payload(p)


@router.get('/payments/{payment_id}')
def payment_status(payment_id: str, session: DB, table_token: str=Query(min_length=20,max_length=100)):
    p=session.get(PlatformAdyenPayment,payment_id)
    if not p:raise HTTPException(404,'Payment not found')
    guest_order(session,p.order_id,table_token)
    return payment_payload(p)


@operator_router.post('/payments/{payment_id}/refunds')
def request_refund(payment_id: str, body: RefundCreate, session: DB, actor: Operator):
    p=session.exec(select(PlatformAdyenPayment).where(PlatformAdyenPayment.id==payment_id).with_for_update()).first()
    if not p or not p.captured_at or not p.psp_reference:raise HTTPException(409,'Only captured payments can be refunded')
    c=session.get(PlatformAdyenConfig,p.environment)
    if not c or c.merchant_account!=p.merchant_account:raise HTTPException(409,'Original Adyen merchant credentials are required')
    refund=session.get(PlatformAdyenRefund,body.idempotency_key)
    if refund:
        if refund.payment_id!=p.id or refund.amount_cents!=body.amount_cents or refund.reason!=body.reason:
            raise HTTPException(409,'Idempotency key was already used for another refund request')
        if refund.state!='creating':return refund.model_dump()
    else:
        previous=session.exec(select(PlatformAdyenRefund).where(PlatformAdyenRefund.payment_id==p.id,
            PlatformAdyenRefund.state.in_(['creating','pending','refunded']))).all()
        amount=sum(r.amount_cents for r in previous)
        if body.amount_cents>p.amount_cents-amount:raise HTTPException(409,'Refund exceeds the unrefunded payment amount')
        total_commission=((amount+body.amount_cents)*p.commission_cents+p.amount_cents//2)//p.amount_cents
        fee=total_commission-sum(r.commission_cents for r in previous)
        refund=PlatformAdyenRefund(id=body.idempotency_key,payment_id=p.id,amount_cents=body.amount_cents,commission_cents=fee,reason=body.reason)
        session.add(refund)
        audit(session,actor,'payment.request_refund',p.id,{'refund_id':refund.id,'amount_cents':body.amount_cents,'reason':body.reason})
        session.commit()
    result=adyen.api(c,'POST','/payments/'+p.psp_reference+'/refunds',key=refund.id,body={
        'merchantAccount':p.merchant_account,'reference':refund.id,'amount':{'currency':p.currency,'value':refund.amount_cents},
        'splits':adyen.splits(p,refund.amount_cents,refund.commission_cents,refund=True)})
    if not result.get('pspReference'):raise HTTPException(502,'Adyen refund response could not be confirmed')
    refund=session.exec(select(PlatformAdyenRefund).where(PlatformAdyenRefund.id==refund.id).with_for_update()).one()
    refund.psp_reference=result['pspReference']
    if refund.state=='creating':refund.state='pending'
    session.add(refund);session.commit();session.refresh(refund);return refund.model_dump()


def reversal_entry(session: Session,p: PlatformAdyenPayment,key: str,kind: str,amount: int,fee: int):
    if session.exec(select(PlatformLedger).where(PlatformLedger.event_key==key)).first():return
    session.add(PlatformLedger(event_key=key,tenant_id=p.tenant_id,order_id=p.order_id,payment_id=p.payment_id,
        currency=p.currency,kind=kind,channel='adyen',amount_cents=amount,commission_bps=p.commission_bps,
        commission_cents=fee,actual_fee_cents=None))


def capture(session: Session,p: PlatformAdyenPayment):
    if p.captured_at:return
    order=session.exec(select(models.Order).where(models.Order.id==p.order_id).with_for_update()).one()
    remaining=order_due_cents(session,order)-amount_paid_cents(session,order.id)
    conflict=remaining!=p.amount_cents or order.deleted_at is not None or order.status==models.OrderStatus.cancelled or order.paid_at is not None
    # Record verified money even if staff changed the bill; surface an explicit reconciliation issue.
    row=models.OrderPayment(tenant_id=p.tenant_id,order_id=p.order_id,amount_cents=p.amount_cents,payment_method='adyen',
        tip_amount_cents=p.tip_cents or None,note='Adyen '+p.psp_reference)
    session.execute(text("SELECT set_config('app.adyen_payment_id',:id,true)"),{'id':p.id})
    session.add(row);session.flush()
    p.payment_id=row.id;p.captured_at=datetime.now(timezone.utc);p.state='needs_review' if conflict else 'captured'
    if conflict:
        p.review_reason='Captured amount differs from the current order balance; check for changed items or another payment'
        order.flagged_for_review=True;order.flag_reason=p.review_reason
    else:
        order.paid_at=p.captured_at;order.payment_method=settlement_payment_method(list_active_payments(session,order.id))
        order.bill_requested_at=None
        order.status=status_after_full_payment(session,order)
        from . import loyalty_service
        loyalty_service.award_on_order_paid(session,order)
    session.add(order);session.add(p)


def consume_event(session: Session,item: dict,environment: str) -> str:
    code=item.get('eventCode','');psp=item.get('pspReference','');success=str(item.get('success','')).lower()=='true'
    original=item.get('originalReference','');ref=item.get('merchantReference','')
    event_id=hashlib.sha256((environment+'|'+psp+'|'+code+'|'+str(success)).encode()).hexdigest()
    # Serialize duplicate notifications even when the provider retries concurrently.
    session.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':int(event_id[:15],16)})
    if session.get(PlatformWebhookEvent,event_id):return 'duplicate'
    p=session.exec(select(PlatformAdyenPayment).where(or_(PlatformAdyenPayment.id==ref,
        PlatformAdyenPayment.psp_reference==original if original else False,
        PlatformAdyenPayment.psp_reference==psp)).with_for_update()).first()
    refund=session.exec(select(PlatformAdyenRefund).where(or_(PlatformAdyenRefund.id==ref,
        PlatformAdyenRefund.psp_reference==psp))).first()
    if not p and refund:
        p=session.exec(select(PlatformAdyenPayment).where(PlatformAdyenPayment.id==refund.payment_id).with_for_update()).first()
    outcome='unrelated'
    if p:
        if p.environment!=environment or item.get('merchantAccountCode')!=p.merchant_account:
            raise HTTPException(400,'Payment merchant or environment mismatch')
        value=(item.get('amount') or {}).get('value');currency=(item.get('amount') or {}).get('currency')
        if not isinstance(value,int) or isinstance(value,bool) or value<0 or currency!=p.currency:
            raise HTTPException(400,'Payment amount or currency mismatch')
        outcome=code.lower()
        if code=='AUTHORISATION':
            if value!=p.amount_cents:raise HTTPException(400,'Authorised amount mismatch')
            if p.psp_reference and p.psp_reference!=psp:raise HTTPException(400,'Payment PSP reference mismatch')
            p.psp_reference=psp
            if not p.captured_at:p.state='authorised' if success else 'failed'
        elif code=='CAPTURE' and success:
            if value!=p.amount_cents:raise HTTPException(400,'Captured amount mismatch')
            if not original:raise HTTPException(400,'Capture originalReference is required')
            if p.psp_reference and p.psp_reference!=original:raise HTTPException(400,'Capture reference mismatch')
            p.psp_reference=original;capture(session,p)
        elif code in ('REFUND','REFUND_FAILED','REFUNDED_REVERSED'):
            if not p.captured_at:raise HTTPException(409,'Capture has not been recorded yet; retry this notification')
            if not refund:
                if code!='REFUND' or not success:
                    outcome='unknown_refund'
                else:
                    previous=session.exec(select(PlatformAdyenRefund).where(PlatformAdyenRefund.payment_id==p.id,
                        PlatformAdyenRefund.state.in_(['creating','pending','refunded']))).all()
                    total=sum(r.amount_cents for r in previous)
                    if value>p.amount_cents-total:raise HTTPException(400,'Refund exceeds recorded payment')
                    fee=((total+value)*p.commission_cents+p.amount_cents//2)//p.amount_cents-sum(r.commission_cents for r in previous)
                    refund=PlatformAdyenRefund(id='external-'+psp,payment_id=p.id,psp_reference=psp,amount_cents=value,commission_cents=fee,reason='Adyen external refund')
            if refund:
                if refund.payment_id!=p.id or value!=refund.amount_cents:raise HTTPException(400,'Refund amount or payment mismatch')
                refund.psp_reference=psp
                if code=='REFUND' and success:
                    if refund.state in ('creating','pending'):
                        reversal_entry(session,p,'refund:'+refund.id,'refund',-refund.amount_cents,-refund.commission_cents)
                        refund.state='refunded'
                else:
                    if refund.state=='refunded':
                        reversal_entry(session,p,'refund-reversal:'+refund.id,'refund_reversal',refund.amount_cents,refund.commission_cents)
                        refund.state='reversed'
                    elif refund.state!='reversed':refund.state='reversed' if code=='REFUNDED_REVERSED' else 'failed'
                session.add(refund)
        elif code in ('CHARGEBACK','CHARGEBACK_REVERSED') and success:
            if not p.captured_at or value>p.amount_cents:raise HTTPException(400,'Invalid chargeback amount')
            fee=(value*p.commission_cents+p.amount_cents//2)//p.amount_cents
            direction=-1 if code=='CHARGEBACK' else 1
            reversal_entry(session,p,'event:'+event_id,code.lower(),direction*value,direction*fee)
            p.state='needs_review';p.review_reason='Adyen dispute: '+code
        elif code in ('CAPTURE_FAILED','CANCELLATION','EXPIRE'):
            if p.captured_at:
                p.state='needs_review';p.review_reason='Adyen notification requires reconciliation: '+code
            elif code=='CAPTURE_FAILED' or success:p.state='failed' if code=='CAPTURE_FAILED' else 'cancelled'
        session.add(p)
    session.add(PlatformWebhookEvent(id=event_id,event_code=code,psp_reference=psp,payment_id=p.id if p else None,outcome=outcome))
    return outcome


@router.post('/adyen/webhook',response_class=PlainTextResponse)
async def webhook(request: Request,session: DB):
    raw=await request.body()
    if len(raw)>1048576:raise HTTPException(413,'Webhook body is too large')
    try:body=await request.json()
    except ValueError:raise HTTPException(400,'Invalid webhook JSON') from None
    if not isinstance(body,dict) or body.get('live') not in ('true','false',True,False):raise HTTPException(400,'Webhook environment is required')
    environment='live' if body['live'] in ('true',True) else 'test'
    c=session.get(PlatformAdyenConfig,environment)
    batch=body.get('notificationItems')
    if not c or not isinstance(batch,list) or not 1<=len(batch)<=100:raise HTTPException(400,'Invalid webhook batch')
    items=[]
    for wrapper in batch:
        item=wrapper.get('NotificationRequestItem') if isinstance(wrapper,dict) else None
        if (not isinstance(item,dict) or not item.get('pspReference') or not item.get('eventCode')
            or not isinstance(item.get('amount'),dict) or not isinstance(item.get('additionalData'),dict)
            or not adyen.verify_hmac(item,c)):
            raise HTTPException(401,'Webhook signature is invalid')
        if item.get('merchantAccountCode')!=c.merchant_account:raise HTTPException(400,'Webhook merchant mismatch')
        items.append(item)
    # Verify the complete batch before writing anything. A failure rolls back every item.
    affected=set()
    for item in items:
        outcome=consume_event(session,item,environment)
        if outcome!='duplicate':
            ref=item.get('merchantReference','')
            p=session.get(PlatformAdyenPayment,ref)
            if p:affected.add((p.tenant_id,p.order_id))
    session.commit()
    # Publish only committed state; never let a notification transport failure undo money records.
    from .main import publish_order_update
    for tenant_id,order_id in affected:
        order=session.get(models.Order,order_id)
        try:
            publish_order_update(tenant_id,{'type':'order_paid' if order.paid_at else 'order_updated',
                'order_id':order_id,'status':order.status.value},table_id=order.table_id)
        except Exception:
            logging.getLogger(__name__).exception('Payment update notification failed for order %s',order_id)
    return '[accepted]'
