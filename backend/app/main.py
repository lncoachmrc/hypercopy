from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text

from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import settings
from app.core.errors import HyperCopyError
from app.core.logging import configure_logging, correlation_id_var, get_logger
from app.db.redis import redis_client
from app.adapters.ratelimit import Budget, WeightedRateLimiter
from app.db.session import SessionLocal
from app.services.metrics import system_snapshot

configure_logging()
log=get_logger(__name__)
app=FastAPI(title='HyperCopy API',version=settings.APP_VERSION,docs_url='/docs' if settings.APP_ENV!='production' else None,redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.PUBLIC_APP_URL, 'https://traxion.lucianonovello.com'],
    allow_credentials=True,
    allow_methods=['GET','POST','PUT','DELETE','OPTIONS'],
    allow_headers=['Content-Type','X-CSRF-Token','X-Requested-With','X-Metrics-Token'],
)
app.include_router(health_router)
app.include_router(api_router)


@app.middleware('http')
async def security_and_correlation(request:Request,call_next):
    cid=request.headers.get('X-Correlation-ID') or uuid.uuid4().hex
    token=correlation_id_var.set(cid)
    try:
        response=await call_next(request)
        response.headers['X-Correlation-ID']=cid
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        response.headers['Content-Security-Policy']=("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' "+settings.API_BASE_URL.replace('https://','wss://').replace('http://','ws://')+"; frame-ancestors 'none'; base-uri 'self'; object-src 'none'")
        if settings.APP_ENV!='development': response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        return response
    finally:
        correlation_id_var.reset(token)


@app.exception_handler(HyperCopyError)
async def hypercopy_error(request:Request,exc:HyperCopyError):
    return JSONResponse(status_code=exc.status_code,content={'error':{'code':exc.code,'message':str(exc),'correlation_id':request.headers.get('X-Correlation-ID')}})


@app.exception_handler(Exception)
async def unhandled(request:Request,exc:Exception):
    log.exception('Unhandled API error')
    return JSONResponse(status_code=500,content={'error':{'code':'INTERNAL_ERROR','message':'Unexpected server error','correlation_id':correlation_id_var.get()}})


@app.get('/metrics',include_in_schema=False)
async def metrics(request:Request):
    if settings.APP_ENV=='production' and (not settings.METRICS_TOKEN or request.headers.get('X-Metrics-Token')!=settings.METRICS_TOKEN):
        return PlainTextResponse('not found',status_code=404)
    rate = {}
    try:
        rate = await WeightedRateLimiter(redis_client(), Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN)).snapshot()
    except Exception:
        pass
    async with SessionLocal() as db:
        snap=await system_snapshot(db, rate)
    lines=[
        f'hypercopy_queue_depth {snap["queue_depth"]}',
        f'hypercopy_oldest_job_age_seconds {snap["oldest_job_age_seconds"]}',
        f'hypercopy_unknown_executions {snap["unknown_executions"]}',
        f'hypercopy_reconciliation_failures_1h {snap["reconciliation_failures_1h"]}',
        f'hypercopy_execution_latency_ms_avg_15m {snap["execution_latency_ms_avg_15m"]}',
        f'hypercopy_execution_reject_rate_15m {snap["execution_reject_rate_15m"]}',
        f'hypercopy_credential_expiring_7d {snap["credential_expiring_7d"]}',
    ]
    if snap['watcher_last_event_age_seconds'] is not None:
        lines.append(f'hypercopy_watcher_last_event_age_seconds {snap["watcher_last_event_age_seconds"]}')
    if rate:
        lines.append(f'hypercopy_hl_rate_weight_used {rate.get("used",0)}')
        lines.append(f'hypercopy_hl_rate_weight_used_pct {rate.get("used_pct",0)}')
    try:
        rc=redis_client(); info=await rc.info('memory')
        lines.append(f'hypercopy_redis_used_memory_bytes {info.get("used_memory",0)}')
        lines.append(f'hypercopy_ws_reconnect_count {int(await rc.get("hypercopy:metrics:ws_reconnect_count") or 0)}')
        lines.append(f'hypercopy_hl_429_count {int(await rc.get("hypercopy:metrics:hl_429_count") or 0)}')
    except Exception: pass
    return PlainTextResponse('\n'.join(lines)+'\n',media_type='text/plain; version=0.0.4')
