"""End-to-end checks on a disposable Postgres DB; no external payments or printers.

Run directly with DB_NAME=platform_operations_test. All Adyen calls are mocked.
"""
import os
os.environ['RATE_LIMIT_ENABLED']='false'
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import base64,hashlib,hmac,uuid
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import Enum,text
from sqlmodel import SQLModel,Session,select
from app import models,adyen_service
from app.db import engine
from app.main import app
from app.platform_models import *
from app.security import get_current_platform_operator,create_access_token

assert engine.url.database=='platform_operations_test','Only a disposable database is permitted'
os.environ['PLATFORM_SECRETS_KEY']=Fernet.generate_key().decode()
os.environ['PLATFORM_ALLOW_TEST_PAYMENTS']='true'
with engine.begin() as conn:
    for table in SQLModel.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type,Enum) and column.type.native_enum:column.type.create(conn,checkfirst=True)
SQLModel.metadata.create_all(engine)
migration=Path('/app/migrations/20261029130000_platform_operations.sql').read_text()
with engine.begin() as conn:conn.exec_driver_sql(migration)

client=TestClient(app)
assert client.get('/platform/operations/settings').status_code==401
with Session(engine) as s:
    actor=models.User(email='ops-test@354612.xyz',hashed_password='unused',role=models.UserRole.platform_operator)
    s.add(actor);s.commit();s.refresh(actor);s.expunge(actor)
app.dependency_overrides[get_current_platform_operator]=lambda:actor
BASE='/platform/operations'
def good(response,status=200):
    assert response.status_code==status,(response.status_code,response.text)
    return response.json()

def fixture(price=10000,tip=0,rate=None):
    with Session(engine) as s:
        tenant=models.Tenant(name='Platform isolated test',currency_code='CNY',qr_auto_open=True)
        s.add(tenant);s.flush()
        s.add(PlatformRestaurant(tenant_id=tenant.id,commission_bps=rate,test_balance_account='BA'+uuid.uuid4().hex,test_account_holder='AHrestaurant'))
        floor=models.Floor(name='Test floor',tenant_id=tenant.id);s.add(floor);s.flush()
        table=models.Table(name='A1',tenant_id=tenant.id,floor_id=floor.id,is_active=True);s.add(table);s.flush()
        product=models.Product(name='Test dish',tenant_id=tenant.id,price_cents=price);s.add(product);s.flush()
        order=models.Order(tenant_id=tenant.id,table_id=table.id,tip_amount_cents=tip);s.add(order);s.flush()
        s.add(models.OrderItem(order_id=order.id,product_id=product.id,product_name=product.name,quantity=1,price_cents=price))
        s.commit();return tenant.id,table.token,order.id

def pay(tenant,order,amount,tip=0):
    with Session(engine) as s:
        p=models.OrderPayment(tenant_id=tenant,order_id=order,amount_cents=amount,payment_method='cash',tip_amount_cents=tip)
        s.add(p);s.commit();return p.id

cfg=good(client.get(BASE+'/settings'))
cfg={k:cfg[k] for k in ('name','support_email','support_phone','terms_url','public_base_url','default_commission_bps','estimated_fee_bps','estimated_fee_fixed_cents','payment_enabled','adyen_environment','registration_enabled')}
cfg.update(name='Test platform',default_commission_bps=500,public_base_url='https://drink.354612.xyz')
good(client.put(BASE+'/settings',json=cfg))
assert client.put(BASE+'/settings',json=cfg|{'default_commission_bps':10001}).status_code==422
assert client.put(BASE+'/settings',json=cfg|{'payment_enabled':True}).status_code==409
created=good(client.post(BASE+'/restaurants',json={'name':'Created restaurant','owner_name':'Test owner','owner_email':'created-test@354612.xyz','owner_password':'OnlyForTests-12345','currency':'EUR'}),201)
assert created['currency']=='EUR'
good(client.post(BASE+f"/restaurants/{created['id']}/owner-password",json={'password':'OnlyForTests-67890'}))
print('PASS operator access, restaurant creation, password reset and validated configuration')

tid,token,oid=fixture(101,rate=500)
first=pay(tid,oid,50);pay(tid,oid,51)
with Session(engine) as s:
    rows=s.exec(select(PlatformLedger).where(PlatformLedger.order_id==oid)).all()
    assert sum(x.commission_cents for x in rows)==5
    r=s.get(PlatformRestaurant,tid);r.commission_bps=9000;s.add(r);s.commit()
    firstpay=s.get(models.OrderPayment,first);firstpay.voided_at=datetime.now(timezone.utc);s.add(firstpay);s.commit()
pay(tid,oid,50)
with Session(engine) as s:
    assert s.get(PlatformOrderTerms,oid).commission_bps==500
    rows=s.exec(select(PlatformLedger).where(PlatformLedger.order_id==oid)).all()
    assert sum(x.amount_cents for x in rows)==101 and sum(x.commission_cents for x in rows)==5
tidtip,_,oidtip=fixture(1000,100,rate=1000);pay(tidtip,oidtip,1100,100)
with Session(engine) as s:
    assert s.exec(select(PlatformLedger).where(PlatformLedger.order_id==oidtip)).one().commission_cents==100
before=good(client.get(BASE+'/finance',params={'tenant_id':tid,'currency':'CNY'}))['totals']
with engine.begin() as conn:conn.exec_driver_sql(migration)
assert good(client.get(BASE+'/finance',params={'tenant_id':tid,'currency':'CNY'}))['totals']==before
print('PASS cumulative rounding, tips excluded, frozen rates, void reversal and migration replay')

merchant='TestMerchant';hkey='ab'*32
adyen_cfg={'merchant_account':merchant,'balance_platform':'BPtest','liable_balance_account':'BAplatform','api_key':'only-test-api-key','balance_api_key':'only-test-balance-key','hmac_key':hkey}
saved=good(client.put(BASE+'/adyen/test',json=adyen_cfg))
assert saved['ready'] and 'only-test' not in str(saved) and hkey not in str(saved)
good(client.put(BASE+'/settings',json=cfg|{'payment_enabled':True}))
with Session(engine) as s:
    savedrow=s.get(PlatformAdyenConfig,'test')
    assert savedrow.api_key_encrypted!='only-test-api-key' and adyen_service.decrypt(savedrow.api_key_encrypted)=='only-test-api-key'
    assert 'only-test' not in str(s.exec(select(PlatformAuditLog)).all())

calls=[]
def api(c,method,path,**kw):
    calls.append((path,kw))
    if path=='/paymentLinks':return {'id':'link-'+kw['key'],'url':'https://test.adyen.link/'+kw['key']}
    if path.endswith('/refunds'):return {'pspReference':'refund-'+kw['key']}
    raise AssertionError('Unexpected external request '+path)

def event(pid,code,psp,amount,original='',success=True):
    item={'pspReference':psp,'originalReference':original,'merchantAccountCode':merchant,'merchantReference':pid,
          'eventCode':code,'success':str(success).lower(),'amount':{'currency':'CNY','value':amount}}
    sign=base64.b64encode(hmac.new(bytes.fromhex(hkey),adyen_service.hmac_payload(item).encode(),hashlib.sha256).digest()).decode()
    item['additionalData']={'hmacSignature':sign};return item

def notify(*items):
    return client.post('/platform-payments/adyen/webhook',json={'live':'false','notificationItems':[{'NotificationRequestItem':x} for x in items]})

with patch('app.adyen_service.api',side_effect=api),patch('app.adyen_service.verify_account',return_value={'status':'active'}),patch('app.main.publish_order_update'):
    tenant,table_token,order_id=fixture(10000,200,rate=750)
    payment=good(client.post(f'/platform-payments/orders/{order_id}/link',json={'table_token':table_token}))
    assert payment['amount_cents']==10200
    split=calls[-1][1]['body']['splits']
    assert sum(x.get('amount',{}).get('value',0) for x in split)==10200
    assert next(x for x in split if x['type']=='Commission')['amount']['value']==750
    repeat=good(client.post(f'/platform-payments/orders/{order_id}/link',json={'table_token':table_token}))
    assert repeat['id']==payment['id'] and len(calls)==1
    assert client.post(f'/platform-payments/orders/{order_id}/link',json={'table_token':token}).status_code==404
    assert client.get('/platform-payments/payments/'+payment['id'],params={'table_token':token}).status_code==404
    assert client.put(BASE+'/adyen/test',json=adyen_cfg|{'merchant_account':'Other'}).status_code==409
    auth=event(payment['id'],'AUTHORISATION','auth-1',10200)
    corrupted=dict(auth);corrupted['amount']={'currency':'CNY','value':1}
    assert notify(corrupted).status_code==401
    assert notify(auth).status_code==200
    with Session(engine) as s:assert s.get(models.Order,order_id).paid_at is None
    cap=event(payment['id'],'CAPTURE','capture-1',10200,'auth-1')
    with ThreadPoolExecutor(max_workers=3) as pool:
        results=list(pool.map(lambda _:notify(cap),range(3)))
    assert all(x.status_code==200 for x in results),[(x.status_code,x.text) for x in results]
    with Session(engine) as s:
        p=s.get(PlatformAdyenPayment,payment['id']);assert p.state=='captured' and p.captured_at
        assert s.get(models.Order,order_id).paid_at
        assert len(s.exec(select(models.OrderPayment).where(models.OrderPayment.order_id==order_id)).all())==1
        assert s.exec(select(PlatformLedger).where(PlatformLedger.order_id==order_id)).one().commission_cents==750
    print('PASS encrypted secrets, no secret readback, ownership checks, hosted split, HMAC, authorisation and concurrent capture idempotency')
    ref_id=uuid.uuid4().hex
    refund_body={'amount_cents':5100,'reason':'Isolated refund test','idempotency_key':ref_id}
    refund=good(client.post(BASE+'/payments/'+payment['id']+'/refunds',json=refund_body))
    assert refund['commission_cents']==375 and refund['state']=='pending'
    n=len(calls);assert good(client.post(BASE+'/payments/'+payment['id']+'/refunds',json=refund_body))['id']==ref_id and len(calls)==n
    assert client.post(BASE+'/payments/'+payment['id']+'/refunds',json=refund_body|{'amount_cents':999999}).status_code==409
    r=event(ref_id,'REFUND','refund-'+ref_id,5100,'auth-1')
    assert notify(r).status_code==200 and notify(r).status_code==200
    totals=good(client.get(BASE+'/finance',params={'tenant_id':tenant}))['totals']
    assert totals['net_sales_cents']==5100 and totals['commission_cents']==375
    reversal=event(ref_id,'REFUNDED_REVERSED','refund-'+ref_id,5100,'auth-1')
    assert notify(reversal).status_code==200
    totals=good(client.get(BASE+'/finance',params={'tenant_id':tenant}))['totals']
    assert totals['net_sales_cents']==10200 and totals['commission_cents']==750
    # Failed/reversed refund arrives before its earlier success notification.
    ref2=uuid.uuid4().hex
    good(client.post(BASE+'/payments/'+payment['id']+'/refunds',json=refund_body|{'idempotency_key':ref2}))
    assert notify(event(ref2,'REFUNDED_REVERSED','refund-'+ref2,5100,'auth-1')).status_code==200
    assert notify(event(ref2,'REFUND','refund-'+ref2,5100,'auth-1')).status_code==200
    assert good(client.get(BASE+'/finance',params={'tenant_id':tenant}))['totals']['net_sales_cents']==10200
    print('PASS refund limits, idempotency, proportional split, refund reversal and reordered notifications')
    # Capture before authorisation still settles once; amount conflicts are explicit.
    t2,tok2,o2=fixture(2000,rate=500)
    p2=good(client.post(f'/platform-payments/orders/{o2}/link',json={'table_token':tok2}))
    pay(t2,o2,2000)
    assert notify(event(p2['id'],'CAPTURE','capture-2',2000,'auth-2')).status_code==200
    assert notify(event(p2['id'],'AUTHORISATION','auth-2',2000)).status_code==200
    with Session(engine) as s:assert s.get(PlatformAdyenPayment,p2['id']).state=='needs_review'
    # Any invalid signature rejects the entire batch.
    t3,tok3,o3=fixture(800)
    p3=good(client.post(f'/platform-payments/orders/{o3}/link',json={'table_token':tok3}))
    assert notify(event(p3['id'],'AUTHORISATION','auth-3',800),corrupted).status_code==401
    with Session(engine) as s:assert s.get(PlatformAdyenPayment,p3['id']).state=='pending'
    print('PASS payment conflict review, reordered authorisation and atomic webhook batch verification')

restaurant=good(client.get(BASE+f'/restaurants/{tenant}'))
fields=('name','email','phone','address','city','status','commission_bps','notes','recipient_name','bank_name','account_last4','test_balance_account','live_balance_account','test_account_holder','live_account_holder','test_transfer_instrument','live_transfer_instrument')
body={k:restaurant[k] for k in fields};body['status']='suspended'
good(client.put(BASE+f'/restaurants/{tenant}',json=body))
assert client.get('/menu/'+table_token).status_code==403
assert client.get('/public/tenants/'+str(tenant)).status_code==403
assert client.post(f'/platform-payments/orders/{order_id}/link',json={'table_token':table_token}).status_code==403
assert client.post(f'/orders/{order_id}/payments',json={'payment_method':'adyen'}).status_code==400
good(client.put(BASE+'/settings',json=cfg|{'registration_enabled':False}))
assert client.post('/register',json={}).status_code==403
assert client.get(BASE+'/ledger/export').status_code==200
assert good(client.get(BASE+'/audit'))['total']>0
print('PASS suspended restaurant access, forged Adyen rejection, registration switch, CSV and audit')
app.dependency_overrides.clear()
with Session(engine) as s:
    owner=s.exec(select(models.User).where(models.User.tenant_id==created['id'])).one()
    staff_token=create_access_token({'sub':owner.email,'tenant_id':owner.tenant_id,'token_version':owner.token_version})
assert client.get(BASE+'/settings',headers={'Authorization':'Bearer '+staff_token}).status_code==403
assert client.put(BASE+'/settings',json=cfg,headers={'Authorization':'Bearer '+staff_token}).status_code==403
assert notify({'pspReference':'bad','eventCode':'CAPTURE','amount':[],'additionalData':[]}).status_code==401
print('PASS restaurant staff cannot read or change platform configuration; malformed webhook rejected')
print('ALL PLATFORM OPERATIONS CHECKS PASSED')
