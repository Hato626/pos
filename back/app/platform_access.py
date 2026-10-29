"""Apply platform suspension to existing staff and public restaurant routes."""
import re
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from sqlmodel import Session, select
from . import models
from .db import engine
from .settings import settings
from .platform_finance import config, require_active


async def platform_access_middleware(request: Request, call_next):
    path=request.url.path
    root=(settings.root_path or '').rstrip('/')
    if root and path.startswith(root+'/'):path=path[len(root):]
    if request.method=='OPTIONS' or path.startswith(('/platform','/health','/uploads','/docs','/openapi','/logout','/refresh')):
        return await call_next(request)
    tenant_id=None
    token=request.cookies.get('access_token')
    if not token and request.headers.get('authorization','').lower().startswith('bearer '):
        token=request.headers['authorization'][7:]
    if token:
        try:
            payload=jwt.decode(token,settings.secret_key,algorithms=[settings.algorithm])
            tenant_id=payload.get('tenant_id')
        except JWTError:pass
    try:
        with Session(engine) as session:
            if path=='/register' and request.method=='POST' and not config(session).registration_enabled:
                raise HTTPException(403,'Restaurant registration is currently closed')
            table_match=re.match(r'^/menu/([^/]+)',path)
            public_match=re.match(r'^/(?:public/tenants|book|waitlist|feedback|delivery|loyalty)/([^/]+)',path)
            if table_match:
                table=session.exec(select(models.Table).where(models.Table.token==table_match.group(1))).first()
                if table:require_active(session,table.tenant_id)
            elif public_match:
                ref=public_match.group(1)
                tenant=session.get(models.Tenant,int(ref)) if ref.isdigit() else session.exec(select(models.Tenant).where(models.Tenant.public_slug==ref)).first()
                if tenant:require_active(session,tenant.id)
            if tenant_id is not None:require_active(session,int(tenant_id))
            if request.method in ('POST','PUT','PATCH'):
                if request.headers.get('content-type','').startswith('application/json'):
                    try:body=await request.json()
                    except ValueError:body=None
                    if isinstance(body,dict):
                        public_tenant=body.get('tenant_id')
                        if isinstance(public_tenant,int) and not isinstance(public_tenant,bool):require_active(session,public_tenant)
                        if path.startswith('/orders/') and str(body.get('payment_method','')).lower()=='adyen':
                            raise HTTPException(400,'Adyen payments are confirmed by the payment provider')
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail})
    return await call_next(request)
