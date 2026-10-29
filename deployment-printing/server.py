"""Independent deployment extension: transactional events, tenant bindings, USB agent API."""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlmodel import Session
from app import models, print_service
from app.db import engine, get_session
from app.permissions import Permission, require_permission
from workflow import build_payload, build_test_payload

log = logging.getLogger('print-workflow')


def rows(connection, sql, **params):
    return [dict(r) for r in connection.execute(text(sql), params).mappings()]


def convert_events():
    with engine.begin() as connection:
        events = rows(connection, 'SELECT * FROM deployment_print_event WHERE processed_at IS NULL ORDER BY id LIMIT 100 FOR UPDATE SKIP LOCKED')
        groups = OrderedDict()
        for event in events:
            key = (event['tenant_id'], event['order_id'], event['kind'], event['id'] if event['kind'] == 'receipt' else 0)
            groups.setdefault(key, []).append(event)
        for (tenant_id, order_id, kind, _), group in groups.items():
            # An offline sale may insert the already-paid order before its item rows.
            if kind == 'receipt' and not group[-1]['payload']['items']:
                group[-1]['payload'] = connection.execute(text('SELECT deployment_ticket_snapshot(:oid)'), {'oid':order_id}).scalar_one()
            payload = build_payload(group)
            job_id = None
            if payload:
                role = 'receipt' if kind == 'receipt' else 'kitchen'
                job_id = connection.execute(text('''INSERT INTO print_job(tenant_id,order_id,job_type,printer_role,status,payload,created_at)
                    VALUES(:tid,:oid,:role,:role,'pending',CAST(:payload AS jsonb),now()) RETURNING id'''),
                    {'tid':tenant_id,'oid':order_id,'role':role,'payload':json.dumps(payload, ensure_ascii=False)}).scalar_one()
            for event in group:
                connection.execute(text('UPDATE deployment_print_event SET job_id=:jid,processed_at=now() WHERE id=:id'), {'jid':job_id,'id':event['id']})
        return len(events)


async def worker():
    while True:
        try:
            await asyncio.to_thread(convert_events)
        except Exception as error:
            log.error('Event conversion failed (%s); transaction rolled back, retrying', type(error).__name__)
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    with engine.begin() as connection:
        connection.exec_driver_sql(Path(__file__).with_name('schema.sql').read_text())
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
Owner = Annotated[models.User, Depends(require_permission(Permission.SETTINGS_UPDATE))]
Staff = Annotated[models.User, Depends(require_permission(Permission.ORDER_READ))]


def tenant(user):
    if user.tenant_id is None:
        raise HTTPException(403, '请使用餐厅账号')
    return user.tenant_id


def csrf(request: Request):
    if request.headers.get('x-requested-with') != 'SatisfechoPrinting':
        raise HTTPException(403, '缺少请求验证头')
    origin = request.headers.get('origin')
    if origin and origin not in ('https://drink-admin.354612.xyz', 'https://drink.354612.xyz'):
        raise HTTPException(403, '请求来源无效')


def agent_auth(authorization: Annotated[str | None, Header()] = None, session: Session = Depends(get_session)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, '缺少打印助手令牌')
    return print_service.get_agent_by_token(session, authorization[7:])


Agent = Annotated[models.PrintAgent, Depends(agent_auth)]


class Binding(BaseModel):
    agent_id: int | None = None
    kitchen_printer: str = Field(min_length=1, max_length=256)
    receipt_printer: str = Field(min_length=1, max_length=256)
    auto_kitchen: bool = True
    auto_receipt: bool = True


class Device(BaseModel):
    printers: list[str] = Field(max_length=100)
    protocol_version: int = Field(default=1, ge=1, le=2)


class PaperSetting(BaseModel):
    agent_id: int
    printer: str = Field(min_length=1, max_length=256)
    width_mm: int
    height_mm: int

    @model_validator(mode='after')
    def supported_size(self):
        if (self.width_mm,self.height_mm) not in ((50,60),(30,60),(60,50),(60,30),(60,40)):
            raise ValueError('请选择支持的纸张尺寸（宽 × 走纸长度）')
        return self


def paper_for(connection, tid, aid, printer):
    found = rows(connection, '''SELECT width_mm,height_mm FROM deployment_printer_paper
        WHERE tenant_id=:tid AND agent_id=:aid AND printer=:printer''', tid=tid, aid=aid, printer=printer)
    return found[0] if found else {'width_mm':60,'height_mm':40}


class Complete(BaseModel):
    status: str
    error_message: str | None = Field(default=None, max_length=500)


class TestPrint(BaseModel):
    job_type: Literal['kitchen', 'receipt']


@app.get('/health')
def health():
    return {'status':'ok'}


@app.get('/binding')
def get_binding(user: Owner):
    tid = tenant(user)
    with engine.connect() as connection:
        found = rows(connection, 'SELECT * FROM deployment_printer_binding WHERE tenant_id=:tid', tid=tid)
        devices = rows(connection, '''SELECT a.id,a.display_name,a.revoked_at,a.last_seen_at,d.printers,d.protocol_version
            FROM print_agent a LEFT JOIN deployment_print_device d ON d.agent_id=a.id
            WHERE a.tenant_id=:tid AND a.revoked_at IS NULL ORDER BY a.id''', tid=tid)
        papers = rows(connection, 'SELECT agent_id,printer,width_mm,height_mm FROM deployment_printer_paper WHERE tenant_id=:tid ORDER BY agent_id,printer', tid=tid)
    return {'binding':found[0] if found else {'agent_id':None,'kitchen_printer':'Deli DL-720C','receipt_printer':'Deli DL-720C','auto_kitchen':False,'auto_receipt':False}, 'devices':devices, 'paper_settings':papers}


@app.put('/paper-settings', dependencies=[Depends(csrf)])
def save_paper(body: PaperSetting, user: Owner):
    tid = tenant(user)
    with engine.begin() as connection:
        devices = rows(connection, '''SELECT d.printers FROM print_agent a
            LEFT JOIN deployment_print_device d ON d.agent_id=a.id
            WHERE a.id=:aid AND a.tenant_id=:tid AND a.revoked_at IS NULL''', aid=body.agent_id, tid=tid)
        if not devices:
            raise HTTPException(404, '打印助手不属于本餐厅或已撤销')
        bound = rows(connection, '''SELECT 1 FROM deployment_printer_binding WHERE tenant_id=:tid AND agent_id=:aid
            AND (kitchen_printer=:printer OR receipt_printer=:printer)''', tid=tid, aid=body.agent_id, printer=body.printer)
        if body.printer not in (devices[0]['printers'] or []) and not bound:
            raise HTTPException(409, '请选择助手上报的实际打印队列')
        connection.execute(text('''INSERT INTO deployment_printer_paper(tenant_id,agent_id,printer,width_mm,height_mm)
            VALUES(:tid,:agent_id,:printer,:width_mm,:height_mm)
            ON CONFLICT(tenant_id,agent_id,printer) DO UPDATE SET width_mm=excluded.width_mm,height_mm=excluded.height_mm'''), {'tid':tid,**body.model_dump()})
    return {'saved':True,**body.model_dump()}


@app.get('/paper-profiles')
def paper_profiles(user: Staff):
    tid = tenant(user)
    profiles = []
    with engine.connect() as connection:
        bound = rows(connection, 'SELECT * FROM deployment_printer_binding WHERE tenant_id=:tid',tid=tid)
        devices = rows(connection, '''SELECT a.id,a.display_name,d.printers FROM print_agent a
            JOIN deployment_print_device d ON d.agent_id=a.id WHERE a.tenant_id=:tid AND a.revoked_at IS NULL ORDER BY a.id''', tid=tid)
        for device in devices:
            printers = []
            if bound and bound[0]['agent_id']==device['id']:
                printers.extend([bound[0]['kitchen_printer'],bound[0]['receipt_printer']])
            printers.extend(p['printer'] for p in rows(connection,'SELECT printer FROM deployment_printer_paper WHERE tenant_id=:tid AND agent_id=:aid ORDER BY printer',tid=tid,aid=device['id']))
            for printer in dict.fromkeys(printers):
                if printer not in device['printers']:
                    continue
                profiles.append({'agent_id':device['id'],'display_name':device['display_name'],'printer':printer,
                                 **paper_for(connection,tid,device['id'],printer)})
    return {'profiles':profiles}


@app.put('/binding', dependencies=[Depends(csrf)])
def save_binding(body: Binding, user: Owner):
    tid = tenant(user)
    with engine.begin() as connection:
        if body.agent_id is not None and not rows(connection, 'SELECT id FROM print_agent WHERE id=:aid AND tenant_id=:tid AND revoked_at IS NULL', aid=body.agent_id, tid=tid):
            raise HTTPException(404, '打印助手不属于本餐厅或已撤销')
        connection.execute(text('''INSERT INTO deployment_printer_binding(tenant_id,agent_id,kitchen_printer,receipt_printer,auto_kitchen,auto_receipt)
            VALUES(:tid,:agent_id,:kitchen_printer,:receipt_printer,:auto_kitchen,:auto_receipt)
            ON CONFLICT(tenant_id) DO UPDATE SET agent_id=excluded.agent_id,kitchen_printer=excluded.kitchen_printer,
            receipt_printer=excluded.receipt_printer,auto_kitchen=excluded.auto_kitchen,auto_receipt=excluded.auto_receipt'''), {'tid':tid, **body.model_dump()})
    return {'saved':True}


@app.post('/devices', dependencies=[Depends(csrf)])
def create_device(user: Owner, session: Session = Depends(get_session)):
    import secrets
    agent, token = print_service.create_agent(session, tenant_id=tenant(user), device_id='windows-' + secrets.token_hex(6), display_name='Windows USB 打印助手')
    return {'id':agent.id,'token':token}


@app.post('/test-print', dependencies=[Depends(csrf)])
def test_print(body: TestPrint, user: Owner):
    tid = tenant(user)
    with engine.begin() as connection:
        # Serialize test submissions per restaurant so repeated clicks reuse an outstanding task.
        bindings = rows(connection, 'SELECT * FROM deployment_printer_binding WHERE tenant_id=:tid FOR UPDATE', tid=tid)
        if not bindings or bindings[0]['agent_id'] is None:
            raise HTTPException(409, '请先连接打印助手并保存绑定，再测试打印')
        binding = bindings[0]
        devices = rows(connection, '''SELECT d.printers,d.last_seen_at,d.protocol_version FROM print_agent a
            JOIN deployment_print_device d ON d.agent_id=a.id
            WHERE a.id=:aid AND a.tenant_id=:tid AND a.revoked_at IS NULL''', aid=binding['agent_id'], tid=tid)
        if not devices or (datetime.now(timezone.utc) - devices[0]['last_seen_at']).total_seconds() >= 60:
            raise HTTPException(409, '打印助手离线，请在餐厅电脑启动助手，连接后重试')
        queue = binding['kitchen_printer' if body.job_type == 'kitchen' else 'receipt_printer']
        if queue not in devices[0]['printers']:
            raise HTTPException(409, '助手未找到绑定的打印队列，请选择实际 Windows 队列并保存')
        paper = paper_for(connection,tid,binding['agent_id'],queue)
        if paper != {'width_mm':60,'height_mm':40} and devices[0]['protocol_version'] < 2:
            raise HTTPException(409, '请下载新版桌面包，关闭旧助手后启动新版，再按后台纸张尺寸测试')
        pending = rows(connection, '''SELECT id FROM print_job WHERE tenant_id=:tid AND job_type=:kind
            AND payload->>'is_test'='true' AND status IN ('pending','claimed') ORDER BY id DESC LIMIT 1''', tid=tid, kind=body.job_type)
        if pending:
            return {'id':pending[0]['id'], 'already_queued':True}
        restaurant = rows(connection, 'SELECT name,currency_code,timezone FROM tenant WHERE id=:tid', tid=tid)[0]
        payload = build_test_payload(restaurant, body.job_type, datetime.now(timezone.utc))
        job_id = connection.execute(text('''INSERT INTO print_job(tenant_id,order_id,job_type,printer_role,status,payload,created_at,created_by_user_id)
            VALUES(:tid,NULL,:kind,:kind,'pending',CAST(:payload AS jsonb),now(),:uid) RETURNING id'''),
            {'tid':tid, 'kind':body.job_type, 'uid':user.id, 'payload':json.dumps(payload,ensure_ascii=False)}).scalar_one()
    return {'id':job_id, 'already_queued':False}


@app.post('/agent/heartbeat')
def heartbeat(body: Device, agent: Agent, session: Session = Depends(get_session)):
    printers = list(dict.fromkeys(p.strip() for p in body.printers if p.strip() and len(p) <= 256))
    print_service.touch_agent(session, agent)
    with engine.begin() as connection:
        connection.execute(text('''INSERT INTO deployment_print_device(agent_id,printers,last_seen_at,protocol_version) VALUES(:aid,CAST(:printers AS jsonb),now(),:version)
            ON CONFLICT(agent_id) DO UPDATE SET printers=excluded.printers,last_seen_at=now(),protocol_version=excluded.protocol_version'''), {'aid':agent.id,'printers':json.dumps(printers),'version':body.protocol_version})
        bindings = rows(connection, 'SELECT * FROM deployment_printer_binding WHERE tenant_id=:tid AND agent_id=:aid', tid=agent.tenant_id, aid=agent.id)
    return {'bound':bool(bindings), 'binding':bindings[0] if bindings else None}


@app.get('/agent/jobs')
def claim(agent: Agent, protocol_version: Annotated[int, Query(ge=1,le=2)] = 1):
    with engine.begin() as connection:
        binding = rows(connection, 'SELECT * FROM deployment_printer_binding WHERE tenant_id=:tid AND agent_id=:aid', tid=agent.tenant_id, aid=agent.id)
        if not binding:
            return []
        jobs = rows(connection, "SELECT * FROM print_job WHERE tenant_id=:tid AND status='pending' ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED", tid=agent.tenant_id)
        for job in jobs:
            # Resolve bindings when claiming; rebinding never moves a job that is already printing.
            queue = binding[0]['kitchen_printer' if job['job_type'] == 'kitchen' else 'receipt_printer']
            paper = paper_for(connection,agent.tenant_id,agent.id,queue)
            if protocol_version < 2 and paper != {'width_mm':60,'height_mm':40}:
                return []  # Old assistants hardcode 60x40; never hand them a differently sized ticket.
            payload = job['payload']
            if payload.get('workflow') != 1 and job['order_id']:
                snapshot = connection.execute(text('SELECT deployment_ticket_snapshot(:oid)'), {'oid':job['order_id']}).scalar_one()
                event = {'id':0,'kind':'receipt' if job['job_type'] == 'receipt' else 'cancel','payload':snapshot}
                payload = build_payload([event]) or payload
                if job['job_type'] == 'kitchen':
                    payload['title'] = '后厨单 · 补打'
                    payload['lines'] = [{**line,'action':'制作'} for line in payload.get('lines',[])]
                elif not snapshot['order'].get('paid_at'):
                    payload['title'] = '账单 · 待付款'
            payload = {**payload,'paper':paper,'windows_printer':queue}
            if payload.get('is_test'):
                payload['order_notes'] = f"纸张 {paper['width_mm']}×{paper['height_mm']} mm\n仅测试，不制作、不收款"
            connection.execute(text("UPDATE print_job SET status='claimed',claimed_by_agent_id=:aid,claimed_at=now(),payload=CAST(:payload AS jsonb) WHERE id=:id"), {'aid':agent.id,'id':job['id'],'payload':json.dumps(payload,ensure_ascii=False)})
            job.update(payload=payload, windows_printer=queue)
        return jobs


@app.post('/agent/jobs/{job_id}/complete')
def complete(job_id: int, body: Complete, agent: Agent):
    if body.status not in ('done','failed'):
        raise HTTPException(400, '状态无效')
    with engine.begin() as connection:
        found = rows(connection, 'SELECT * FROM print_job WHERE id=:id AND tenant_id=:tid FOR UPDATE', id=job_id, tid=agent.tenant_id)
        if not found:
            raise HTTPException(404, '任务不存在')
        job = found[0]
        if job['claimed_by_agent_id'] == agent.id and job['status'] == 'failed' and job['error_message'] == '原任务结果不确定，员工确认后补打':
            # A late acknowledgement must not trap the client after staff explicitly superseded an uncertain job.
            return {'saved':True,'superseded':True}
        if job['claimed_by_agent_id'] != agent.id or job['status'] not in ('claimed',body.status):
            raise HTTPException(409, '任务未由此助手领取')
        connection.execute(text('UPDATE print_job SET status=:status,completed_at=now(),error_message=:error WHERE id=:id'), {'status':body.status,'error':body.error_message if body.status=='failed' else None,'id':job_id})
    return {'saved':True}


@app.get('/jobs')
def list_jobs(user: Staff):
    with engine.connect() as connection:
        return rows(connection, '''SELECT id,order_id,job_type,status,created_at,claimed_at,error_message,
            coalesce(payload->>'is_test','false')='true' AS is_test,
            payload->'paper' AS paper,
            payload->>'windows_printer' AS printer,payload->>'title' AS title
            FROM print_job WHERE tenant_id=:tid ORDER BY id DESC LIMIT 50''', tid=tenant(user))


@app.post('/jobs/{job_id}/reprint', dependencies=[Depends(csrf)])
def reprint(job_id: int, user: Staff):
    with engine.begin() as connection:
        found = rows(connection, 'SELECT * FROM print_job WHERE tenant_id=:tid AND id=:id FOR UPDATE', tid=tenant(user), id=job_id)
        if not found:
            raise HTTPException(404, '任务不存在')
        job = found[0]
        if job['status'] == 'pending':
            raise HTTPException(409, '任务还在等待，请勿重复提交')
        if job['status'] == 'claimed' and (datetime.now(timezone.utc) - job['claimed_at']).total_seconds() < 120:
            raise HTTPException(409, '任务正在打印，请稍后确认实际出纸情况')
        payload = {**job['payload'],'reprint_of':job_id}
        payload['title'] = (payload.get('title') or '小票') + ' · 补打'
        new_id = connection.execute(text('''INSERT INTO print_job(tenant_id,order_id,job_type,printer_role,status,payload,created_at,created_by_user_id)
            VALUES(:tid,:oid,:kind,:role,'pending',CAST(:payload AS jsonb),now(),:uid) RETURNING id'''), {'tid':user.tenant_id,'oid':job['order_id'],'kind':job['job_type'],'role':job['printer_role'],'uid':user.id,'payload':json.dumps(payload,ensure_ascii=False)}).scalar_one()
        if job['status']=='claimed':
            connection.execute(text("UPDATE print_job SET status='failed',error_message='原任务结果不确定，员工确认后补打' WHERE id=:id"), {'id':job_id})
    return {'id':new_id}
