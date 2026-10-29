"""Integration checks against an isolated disposable PostgreSQL database."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.db import engine
from app.models import UserRole
from app.security import get_current_user
import server
from workflow import build_payload

# This database is disposable; never point the test at the application's database.
assert engine.url.database == 'printing_test', 'Refusing to test against a non-test database'
ddl = '''
CREATE TABLE tenant(id serial PRIMARY KEY,name text,currency_code text,timezone text);
CREATE TABLE "table"(id serial PRIMARY KEY,name text);
CREATE TABLE "order"(id serial PRIMARY KEY,tenant_id integer,table_id integer,customer_name text,notes text,
 created_at timestamptz DEFAULT now(),paid_at timestamptz,payment_method text,status text DEFAULT 'pending',
 tip_amount_cents integer,loyalty_discount_cents integer,delivery_fee_cents integer DEFAULT 0,
 order_channel text DEFAULT 'table',deleted_at timestamptz);
CREATE TABLE orderitem(id serial PRIMARY KEY,order_id integer,product_name text,quantity integer,price_cents integer,
 notes text,customization_summary text,line_modifiers_summary text,removed_by_customer boolean DEFAULT false,
 removed_by_user_id integer,status text DEFAULT 'pending');
CREATE TABLE order_payment(id serial PRIMARY KEY,order_id integer,amount_cents integer,payment_method text,voided_at timestamptz);
CREATE TABLE print_agent(id serial PRIMARY KEY,tenant_id integer,device_id varchar(64),display_name varchar(120),token_hash text,
 last_seen_at timestamptz,revoked_at timestamptz,created_at timestamptz DEFAULT now());
CREATE TABLE print_job(id serial PRIMARY KEY,tenant_id integer,order_id integer,job_type varchar(16),printer_role varchar(32),
 status varchar(16),payload jsonb,created_by_user_id integer,claimed_by_agent_id integer,claimed_at timestamptz,
 completed_at timestamptz,error_message varchar(500),created_at timestamptz DEFAULT now());
INSERT INTO tenant VALUES(1,'测试餐厅甲','CNY','Asia/Shanghai'),(2,'测试餐厅乙','CNY','Asia/Shanghai');
'''
with engine.begin() as c:
    c.exec_driver_sql(ddl)


def sql(statement, **params):
    with engine.begin() as c:
        result = c.execute(text(statement), params)
        return [dict(r) for r in result.mappings()] if result.returns_rows else []


async def idle_worker():
    await asyncio.Event().wait()


server.worker = idle_worker
current = SimpleNamespace(id=101,tenant_id=1,role=UserRole.owner)
server.app.dependency_overrides[get_current_user] = lambda: current
headers = {'X-Requested-With':'SatisfechoPrinting'}
with TestClient(server.app) as client:
    assert client.put('/binding', json={'kitchen_printer':'Deli DL-720C','receipt_printer':'Deli DL-720C'}).status_code == 403
    assert client.put('/binding',headers=headers,json={'kitchen_printer':'Deli DL-720C','receipt_printer':'Deli DL-720C'}).status_code == 200
    first = client.post('/devices',headers=headers,json={}).json()
    auth = {'Authorization':'Bearer '+first['token']}
    assert not client.post('/agent/heartbeat',headers=auth,json={'printers':['Deli DL-720C']}).json()['bound']
    assert client.put('/binding',headers=headers,json={'agent_id':first['id'],'kitchen_printer':'Deli DL-720C','receipt_printer':'Deli DL-720C'}).status_code == 200
    assert client.post('/agent/heartbeat',headers=auth,json={'printers':['Deli DL-720C']}).json()['bound']
    current.tenant_id = 2
    second = client.post('/devices',headers=headers,json={}).json()
    assert client.put('/binding',headers=headers,json={'agent_id':first['id'],'kitchen_printer':'Deli DL-720C','receipt_printer':'Deli DL-720C'}).status_code == 404
    assert client.get('/binding').json()['binding']['agent_id'] is None
    current.tenant_id = 1
    print('PASS tenant-isolated bindings, pairing, inventory, and CSRF')

    sql('''INSERT INTO "order"(id,tenant_id,notes) VALUES(1,1,'少盐\n[PAID:internal]');
      INSERT INTO orderitem(order_id,product_name,quantity,price_cents,notes) VALUES(1,'牛肉饭',1,2800,'少辣'),(1,'柠檬茶',2,1200,'少冰');''')
    assert len(sql('SELECT * FROM deployment_print_event')) == 2
    server.convert_events()
    jobs = sql('SELECT * FROM print_job ORDER BY id')
    assert len(jobs) == 1 and jobs[0]['job_type'] == 'kitchen'
    assert [i['quantity'] for i in jobs[0]['payload']['lines']] == [1,2]
    assert jobs[0]['payload']['total_cents'] == 0
    assert '[PAID:' not in jobs[0]['payload']['order_notes']
    server.convert_events()
    assert len(sql('SELECT id FROM print_job')) == 1
    # Payment legs alone never issue a fully settled receipt.
    sql("INSERT INTO order_payment(order_id,amount_cents,payment_method) VALUES(1,1000,'cash')")
    server.convert_events()
    assert len(sql('SELECT id FROM print_job')) == 1
    sql("UPDATE orderitem SET quantity=3 WHERE id=2")
    server.convert_events()
    assert sql('SELECT payload FROM print_job ORDER BY id DESC LIMIT 1')[0]['payload']['lines'][0]['quantity'] == 1
    sql("UPDATE orderitem SET notes='不加糖' WHERE id=2")
    server.convert_events()
    assert [i['action'] for i in sql('SELECT payload FROM print_job ORDER BY id DESC LIMIT 1')[0]['payload']['lines']] == ['撤销原项','改菜']
    sql('''UPDATE "order" SET paid_at=now(),payment_method='cash',tip_amount_cents=100,loyalty_discount_cents=200 WHERE id=1''')
    server.convert_events()
    paid = sql("SELECT * FROM print_job WHERE job_type='receipt'")
    assert len(paid) == 1 and paid[0]['payload']['total_cents'] == 6300
    sql('''UPDATE "order" SET status='completed' WHERE id=1''')
    server.convert_events()
    assert len(sql("SELECT * FROM print_job WHERE job_type='receipt'")) == 1
    before = len(sql('SELECT * FROM deployment_print_event'))
    with engine.connect() as c:
        transaction = c.begin()
        c.execute(text("INSERT INTO orderitem(order_id,product_name,quantity,price_cents) VALUES(1,'回滚测试',1,100)"))
        transaction.rollback()
    assert len(sql('SELECT * FROM deployment_print_event')) == before
    # A disabled second tenant does not generate any events.
    sql('''INSERT INTO "order"(id,tenant_id) VALUES(2,2); INSERT INTO orderitem(order_id,product_name,quantity,price_cents) VALUES(2,'乙餐厅菜品',1,100)''')
    assert len(sql('SELECT * FROM deployment_print_event')) == before
    print('PASS order submission, delta add-ons, modifications, full settlement, rollback, deduplication, disabled tenant')

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _: client.get('/agent/jobs',headers=auth).json(),range(2)))
    ids = [job['id'] for group in claimed for job in group]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(job['windows_printer']=='Deli DL-720C' for group in claimed for job in group)
    other = {'Authorization':'Bearer '+second['token']}
    assert client.post('/agent/jobs/'+str(ids[0])+'/complete',headers=other,json={'status':'done'}).status_code == 404
    assert client.post('/agent/jobs/'+str(ids[0])+'/complete',headers=auth,json={'status':'done'}).status_code == 200
    assert client.post('/agent/jobs/'+str(ids[0])+'/complete',headers=auth,json={'status':'done'}).status_code == 200
    assert client.post('/jobs/'+str(ids[1])+'/reprint',headers=headers,json={}).status_code == 409
    sql("UPDATE print_job SET claimed_at=now()-interval '3 minutes' WHERE id=:id",id=ids[1])
    assert client.post('/jobs/'+str(ids[1])+'/reprint',headers=headers,json={}).status_code == 200
    assert client.post('/agent/jobs/'+str(ids[1])+'/complete',headers=auth,json={'status':'done'}).json()['superseded']
    assert client.get('/agent/jobs',headers={'Authorization':'Bearer invalid'}).status_code == 401
    print('PASS concurrent claims, same-printer routing, scoped and idempotent acknowledgements, reprint guard, invalid tokens')

    # Test tickets exercise the real queue protocol without business orders or payments.
    baseline = sql('''SELECT (SELECT count(*) FROM "order") AS orders,
        (SELECT count(*) FROM order_payment) AS payments,
        (SELECT count(*) FROM deployment_print_event) AS events''')[0]
    sql("UPDATE print_job SET status='done'")
    sql('UPDATE deployment_printer_binding SET auto_kitchen=false,auto_receipt=false WHERE tenant_id=1')
    assert client.post('/test-print',json={'job_type':'kitchen'}).status_code == 403
    assert client.post('/test-print',headers={**headers,'Origin':'https://invalid.example'},json={'job_type':'kitchen'}).status_code == 403
    assert client.post('/test-print',headers=headers,json={'job_type':'invalid'}).status_code == 422
    current.role = UserRole.waiter
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code == 403
    current.role = UserRole.owner
    current.tenant_id = 2
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code == 409
    current.tenant_id = 1
    sql("UPDATE deployment_print_device SET last_seen_at=now()-interval '70 seconds' WHERE agent_id=:id",id=first['id'])
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code == 409
    sql("UPDATE deployment_print_device SET last_seen_at=now(),printers='[]'::jsonb WHERE agent_id=:id",id=first['id'])
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code == 409
    sql('UPDATE deployment_print_device SET printers=CAST(:printers AS jsonb) WHERE agent_id=:id',printers=json.dumps(['Deli DL-720C']),id=first['id'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post('/test-print',headers=headers,json={'job_type':'kitchen'}),range(2)))
    assert all(response.status_code==200 for response in responses)
    test_kitchen=responses[0].json()['id']
    assert responses[1].json()['id']==test_kitchen
    assert sum(response.json()['already_queued'] for response in responses)==1
    receipt=client.post('/test-print',headers=headers,json={'job_type':'receipt'})
    assert receipt.status_code==200
    test_receipt=receipt.json()['id']
    tests=sql("SELECT * FROM print_job WHERE payload->>'is_test'='true' ORDER BY id")
    assert len(tests)==2 and all(job['order_id'] is None and job['tenant_id']==1 for job in tests)
    assert all(job['payload']['total_cents']==0 and job['payload']['order_id']=='TEST' for job in tests)
    assert tests[0]['payload']['title']=='后厨测试单' and tests[1]['payload']['title']=='结算测试单'
    assert all('不制作' in job['payload']['order_notes'] for job in tests)
    assert any(job['is_test'] for job in client.get('/jobs').json())
    current.tenant_id=2
    assert not any(job['is_test'] for job in client.get('/jobs').json())
    assert client.get('/agent/jobs',headers=other).json()==[]
    current.tenant_id=1
    for expected in (test_kitchen,test_receipt):
        job=client.get('/agent/jobs',headers=auth).json()[0]
        assert job['id']==expected and job['windows_printer']=='Deli DL-720C' and job['payload']['is_test']
        assert client.post('/test-print',headers=headers,json={'job_type':job['job_type']}).json()['id']==expected
        assert client.post('/agent/jobs/'+str(expected)+'/complete',headers=auth,json={'status':'done'}).status_code==200
    assert sql('''SELECT (SELECT count(*) FROM "order") AS orders,
        (SELECT count(*) FROM order_payment) AS payments,
        (SELECT count(*) FROM deployment_print_event) AS events''')[0]==baseline
    # Paper sizes belong to a tenant, computer and queue, never to a kitchen/receipt role.
    paper={'agent_id':first['id'],'printer':'Deli DL-720C','width_mm':50,'height_mm':60}
    assert client.put('/paper-settings',json=paper).status_code==403
    assert client.put('/paper-settings',headers=headers,json={**paper,'width_mm':48}).status_code==422
    assert client.put('/paper-settings',headers=headers,json={**paper,'printer':'unknown'}).status_code==409
    current.role=UserRole.waiter
    assert client.put('/paper-settings',headers=headers,json=paper).status_code==403
    current.role=UserRole.owner
    current.tenant_id=2
    assert client.put('/paper-settings',headers=headers,json=paper).status_code==404
    assert client.post('/agent/heartbeat',headers=other,json={'printers':['Deli DL-720C'],'protocol_version':2}).status_code==200
    assert client.put('/paper-settings',headers=headers,json={**paper,'agent_id':second['id'],'width_mm':30}).status_code==200
    current.tenant_id=1
    assert client.put('/paper-settings',headers=headers,json=paper).status_code==200
    settings=client.get('/binding').json()['paper_settings']
    assert len(settings)==1 and settings[0]['width_mm']==50
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code==409
    assert client.post('/agent/heartbeat',headers=auth,json={'printers':['Deli DL-720C','备用打印机'],'protocol_version':2}).status_code==200
    assert client.put('/paper-settings',headers=headers,json={**paper,'printer':'备用打印机','width_mm':30}).status_code==200
    assert len(client.get('/binding').json()['paper_settings'])==2
    current.role=UserRole.waiter
    profiles=client.get('/paper-profiles').json()['profiles']
    assert [(profile['printer'],profile['width_mm']) for profile in profiles]==[('Deli DL-720C',50),('备用打印机',30)]
    current.role=UserRole.owner
    current.tenant_id=2
    assert client.get('/paper-profiles').json()['profiles'][0]['width_mm']==30
    current.tenant_id=1
    for kind in ('kitchen','receipt'):
        created=client.post('/test-print',headers=headers,json={'job_type':kind})
        assert created.status_code==200
        job_id=created.json()['id']
        assert client.get('/agent/jobs',headers=auth).json()==[]  # Even after v2 heartbeat an old process cannot consume it.
        job=client.get('/agent/jobs?protocol_version=2',headers=auth).json()[0]
        assert job['id']==job_id and job['payload']['paper']=={'width_mm':50,'height_mm':60}
        assert '50×60 mm' in job['payload']['order_notes']
        if kind=='receipt':
            assert client.put('/paper-settings',headers=headers,json={**paper,'width_mm':60,'height_mm':30}).status_code==200
            assert sql('SELECT payload FROM print_job WHERE id=:id',id=job_id)[0]['payload']['paper']=={'width_mm':50,'height_mm':60}
        assert client.post('/agent/jobs/'+str(job_id)+'/complete',headers=auth,json={'status':'done'}).status_code==200
    next_id=client.post('/test-print',headers=headers,json={'job_type':'receipt'}).json()['id']
    next_job=client.get('/agent/jobs?protocol_version=2',headers=auth).json()[0]
    assert next_job['id']==next_id and next_job['payload']['paper']=={'width_mm':60,'height_mm':30}
    assert client.post('/agent/jobs/'+str(next_id)+'/complete',headers=auth,json={'status':'done'}).status_code==200
    assert sql('''SELECT (SELECT count(*) FROM "order") AS orders,
        (SELECT count(*) FROM order_payment) AS payments,
        (SELECT count(*) FROM deployment_print_event) AS events''')[0]==baseline
    print('PASS per-queue and per-tenant paper settings, dimensions and permissions, same-printer sharing, claim snapshots and legacy assistant guard')
    sql('UPDATE print_agent SET revoked_at=now() WHERE id=:id',id=first['id'])
    assert client.put('/paper-settings',headers=headers,json=paper).status_code==404
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code==409
    del server.app.dependency_overrides[get_current_user]
    assert client.post('/test-print',headers=headers,json={'job_type':'kitchen'}).status_code==401
    print('PASS kitchen/receipt test tickets, concurrency deduplication, online queue guards, permissions, tenant isolation, completion; business data unchanged')
print('ALL PRINT WORKFLOW TESTS PASSED; no physical printer used')
