from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.adapters.hyperliquid import AccountSnapshot, HyperliquidAdapter, position_configs
from app.adapters.ratelimit import Budget, Priority, WeightedRateLimiter
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.lease import WatcherLease
from app.db.redis import redis_client
from app.db.session import SessionLocal
from app.db.schema import assert_schema
from app.models.entities import MasterEvent, SystemFlag, SystemIncident
from app.services.copy import persist_master_fill_and_jobs
from app.services.queue import publish_job

configure_logging(); log=get_logger(__name__)
stop=asyncio.Event()


def _stop(*_): stop.set()


def _checkpoint_slug() -> str:
    return f'master_checkpoint:{settings.master_network}:{settings.HYPERLIQUID_MASTER_ADDRESS.lower()}'


def _ai_event_queue() -> str:
    return os.getenv('LLM_EVENT_QUEUE', 'hypercopy:ai:master-events').strip() or 'hypercopy:ai:master-events'


async def _checkpoint(db) -> int:
    row=await db.get(SystemFlag,_checkpoint_slug())
    return int((row.value or {}).get('time_ms',0)) if row else 0


async def _set_checkpoint(db,time_ms:int,event_id:str):
    slug=_checkpoint_slug()
    row=await db.get(SystemFlag,slug,with_for_update=True)
    if not row:
        row=SystemFlag(slug=slug,enabled=True,value={}); db.add(row)
    current=int((row.value or {}).get('time_ms',0))
    if time_ms >= current:
        row.value={'time_ms':time_ms,'exchange_event_id':event_id,'network':settings.master_network}
    await db.commit()


class Watcher:
    def __init__(self):
        self.redis=redis_client()
        self.limiter=WeightedRateLimiter(self.redis,Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))
        self.hl=HyperliquidAdapter(self.limiter,network=settings.master_network)
        self.lease=WatcherLease(SessionLocal,ttl_seconds=settings.WATCHER_LEASE_TTL_SECONDS,renew_seconds=settings.WATCHER_LEASE_RENEW_SECONDS)
        self._snapshot: AccountSnapshot | None = None
        self._snapshot_at=0.0
        self._equity=Decimal(0)
        self._equity_at=0.0

    async def _metric_incr(self,name:str):
        try: await self.redis.incr(f'hypercopy:metrics:{name}')
        except Exception: pass

    async def _publish_ai_trigger(self, payload: dict):
        """Best-effort intelligence signal; trading must never wait for it.

        PostgreSQL master_events remains authoritative. The dedicated AI worker
        also checks PostgreSQL as a fallback, so losing this transient Redis
        notification cannot lose strategy-learning data.
        """
        try:
            queue=_ai_event_queue()
            await self.redis.lpush(queue,json.dumps(payload,separators=(',',':')))
            await self.redis.ltrim(queue,0,9999)
        except Exception:
            log.warning('AI intelligence trigger publish failed; PostgreSQL fallback remains available',exc_info=True)

    async def master_snapshot(self,*,force_refresh:bool=False)->AccountSnapshot:
        """Return a recent verified master snapshot with a short stale bridge.

        Bursty masters can emit many fills in one second. Re-reading
        clearinghouseState for every fill wastes the per-IP budget and can
        starve the very lane needed to prove leverage. Reuse a snapshot for a
        couple of seconds, then refresh. If Hyperliquid is transiently 5xx/rate
        limited, a last-known-good snapshot may bridge only the configured short
        stale window; after that the normal fail-closed path takes over.
        """
        now=asyncio.get_running_loop().time()
        age=now-self._snapshot_at if self._snapshot is not None else float('inf')
        if not force_refresh and self._snapshot is not None and age<=settings.HL_MASTER_SNAPSHOT_TTL_SECONDS:
            await self._metric_incr('master_snapshot_cache_hit_count')
            return self._snapshot
        try:
            snapshot=await self.hl.account_snapshot(settings.HYPERLIQUID_MASTER_ADDRESS,priority=Priority.MASTER_STATE)
        except Exception:
            if self._snapshot is not None and age<=settings.HL_MASTER_SNAPSHOT_STALE_SECONDS:
                await self._metric_incr('master_snapshot_stale_fallback_count')
                log.warning('Using recent cached master snapshot while live refresh is unavailable',extra={'age_seconds':round(age,3)})
                return self._snapshot
            await self._metric_incr('master_snapshot_unavailable_count')
            raise
        self._snapshot=snapshot
        self._snapshot_at=now
        self._equity=snapshot.account_value
        self._equity_at=now
        await self._metric_incr('master_snapshot_refresh_count')
        return snapshot

    async def _persisted_master_equity(self)->Decimal:
        now=asyncio.get_running_loop().time()
        if self._equity>0 and now-self._equity_at<=settings.HL_MASTER_SNAPSHOT_STALE_SECONDS:
            return self._equity
        async with SessionLocal() as db:
            last=(await db.execute(select(MasterEvent).order_by(MasterEvent.event_ts.desc()).limit(1))).scalar_one_or_none()
            if last and last.master_equity and last.master_equity>0:
                self._equity=last.master_equity; self._equity_at=now
                await self._metric_incr('master_equity_persisted_fallback_count')
                log.warning('Using last persisted master equity while live refresh is unavailable')
                return self._equity
        raise RuntimeError('Master equity unavailable and no persisted fallback exists')

    async def master_equity(self)->Decimal:
        try:
            return (await self.master_snapshot()).account_value
        except Exception:
            return await self._persisted_master_equity()

    async def process_fill(self,fill:dict):
        asset=str(fill.get('coin') or '')
        config=None
        try:
            snapshot=await self.master_snapshot()
            configs=position_configs(snapshot.perp_state)
            config=configs.get(asset)
            # A fresh cache can predate the very first fill that opens a new
            # asset. Force one refresh when the asset is absent before deciding
            # leverage is unavailable.
            if config is None:
                snapshot=await self.master_snapshot(force_refresh=True)
                config=position_configs(snapshot.perp_state).get(asset)
            equity=snapshot.account_value
        except Exception:
            equity=await self._persisted_master_equity()

        if config is None:
            await self._metric_incr('master_leverage_unavailable_count')
            log.warning('Master leverage unavailable for realtime fill; increasing exposure will wait for reconciliation',extra={'asset':asset})

        cid=uuid.uuid4().hex
        ai_payload=None
        async with SessionLocal() as db:
            event,jobs=await persist_master_fill_and_jobs(
                db,fill=fill,master_equity=equity,fencing_token=self.lease.token,
                correlation_id=cid,source_network=settings.master_network,
                master_leverage=config.leverage if config else None,
                master_is_cross=config.is_cross if config else None,
            )
            if not event: return
            for job in jobs:
                try: await publish_job(self.redis,db,job)
                except Exception: log.warning('Redis publish failed; durable job remains in PostgreSQL',extra={'job_id':str(job.id)},exc_info=True)
            await db.commit()
            await _set_checkpoint(db,int(event.event_ts.timestamp()*1000),event.exchange_event_id)
            ai_payload={
                'master_event_id':str(event.id),
                'exchange_event_id':event.exchange_event_id,
                'asset':event.asset,
                'event_ts':event.event_ts.isoformat(),
                'network':settings.master_network,
            }

        # Fire-and-forget by design. Copy jobs were already persisted/published,
        # so an unavailable AI subsystem cannot add latency to trading.
        if ai_payload:
            asyncio.create_task(self._publish_ai_trigger(ai_payload))

        try: await self.redis.publish(f'{settings.REALTIME_CHANNEL_PREFIX}:system',json.dumps({'type':'master_fill','asset':event.asset,'price':str(event.price),'size':str(event.size),'at':event.event_ts.isoformat(),'network':settings.master_network}))
        except Exception: pass

    async def replay(self):
        async with SessionLocal() as db: start=await _checkpoint(db)
        if start<=0: start=max(int(datetime.now(UTC).timestamp()*1000)-5*60*1000,0)
        seen=0; cursor=start
        for _ in range(5):
            fills=await self.hl.user_fills_by_time(settings.HYPERLIQUID_MASTER_ADDRESS,cursor)
            fills=sorted(fills,key=lambda x:int(x.get('time',0)))
            new=[f for f in fills if int(f.get('time',0))>=cursor]
            if not new: return
            for fill in new:
                await self.process_fill(fill); seen+=1
            if len(fills)<2000: return
            cursor=max(int(f.get('time',cursor)) for f in fills)+1
            if seen>=settings.HL_REPLAY_MAX_FILLS: break
        async with SessionLocal() as db:
            db.add(SystemIncident(severity='CRITICAL',code='MASTER_REPLAY_GAP_UNPROVEN',message='Historical replay hit configured safety ceiling; manual reconciliation required',context={'start_ms':start,'seen':seen,'network':settings.master_network})); flag=await db.get(SystemFlag,'global_pause')
            if not flag: flag=SystemFlag(slug='global_pause',enabled=True); db.add(flag)
            flag.enabled=True; flag.reason='Master replay continuity could not be proven'; await db.commit()
        raise RuntimeError('Master replay continuity could not be proven')

    async def run_leader(self):
        await self.lease.start_renewal()
        try:
            while not stop.is_set() and not self.lease.lost.is_set():
                await self.replay()
                async for fill in self.hl.master_fills(settings.HYPERLIQUID_MASTER_ADDRESS,stop):
                    if self.lease.lost.is_set() or stop.is_set():
                        break
                    await self.process_fill(fill)
                if not stop.is_set() and not self.lease.lost.is_set():
                    log.info('Master websocket session rotated; replaying before reconnect',extra={'network':settings.master_network})
                    await asyncio.sleep(0.5)
        finally:
            await self.lease.stop_renewal(); await self.lease.release()

    async def run(self):
        async with SessionLocal() as db: await assert_schema(db)
        if not settings.HYPERLIQUID_MASTER_ADDRESS: raise RuntimeError('HYPERLIQUID_MASTER_ADDRESS is required')
        log.info('Master watcher source',extra={'network':settings.master_network,'address':settings.HYPERLIQUID_MASTER_ADDRESS[:8]+'…'})
        while not stop.is_set():
            try:
                if await self.lease.try_acquire(): await self.run_leader()
                else: await asyncio.sleep(2)
            except Exception: log.exception('Watcher leader cycle failed'); await asyncio.sleep(5)


async def main():
    loop=asyncio.get_running_loop()
    for s in (signal.SIGTERM,signal.SIGINT):
        try: loop.add_signal_handler(s,_stop)
        except NotImplementedError: pass
    await Watcher().run()

if __name__=='__main__': asyncio.run(main())