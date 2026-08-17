from __future__ import annotations

import asyncio
import signal
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from app.adapters.hyperliquid import AccountSnapshot, HyperliquidAdapter, position_configs, signed_fill_delta
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


def _signed_position_notionals(perp_state: dict) -> dict[str, Decimal] | None:
    """Extract a complete signed current-notional map from clearinghouseState.

    Hyperliquid provides `positionValue` for open perp positions. Smart AI policy
    validation is fail-closed: if an open position lacks a provable current
    notional, return None so realtime falls back to Exact Ratio.
    """
    out: dict[str, Decimal] = {}
    for row in perp_state.get('assetPositions', []):
        position=row.get('position',row)
        size=Decimal(str(position.get('szi','0') or '0'))
        if size==0:
            continue
        raw=position.get('positionValue')
        if raw in (None,''):
            return None
        value=abs(Decimal(str(raw)))
        if value<=0:
            return None
        asset=str(position.get('coin') or '')
        if not asset:
            return None
        out[asset]=value if size>0 else -value
    return out


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
        # Rolling signed notionals let bursty fills update the live portfolio
        # without re-reading clearinghouseState for every event.
        self._live_notionals: dict[str,Decimal] | None = None
        self._live_notionals_snapshot_at=-1.0

    async def _metric_incr(self,name:str):
        try: await self.redis.incr(f'hypercopy:metrics:{name}')
        except Exception: pass

    async def master_snapshot(self,*,force_refresh:bool=False)->AccountSnapshot:
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

    def _live_master_weights(self,snapshot:AccountSnapshot|None,equity:Decimal,fill:dict)->dict[str,Decimal]|None:
        if snapshot is not None and self._live_notionals_snapshot_at != self._snapshot_at:
            self._live_notionals=_signed_position_notionals(snapshot.perp_state)
            self._live_notionals_snapshot_at=self._snapshot_at
        if self._live_notionals is None or equity<=0:
            return None

        # Apply the fill to the rolling map even when a 2-second cached snapshot
        # is reused, so rapid multi-asset bursts cannot keep a stale tracking
        # error estimate until the next REST refresh.
        asset=str(fill.get('coin') or '')
        try:
            start=Decimal(str(fill.get('startPosition','0') or '0'))
            after=start+signed_fill_delta(fill)
            price=Decimal(str(fill.get('px','0') or '0'))
            if after==0:
                self._live_notionals.pop(asset,None)
            elif price>0 and asset:
                notional=abs(after)*price
                self._live_notionals[asset]=notional if after>0 else -notional
            else:
                return None
        except Exception:
            return None
        return {asset:notional/equity for asset,notional in self._live_notionals.items() if notional!=0}

    async def master_equity(self)->Decimal:
        try:
            return (await self.master_snapshot()).account_value
        except Exception:
            return await self._persisted_master_equity()

    async def process_fill(self,fill:dict):
        asset=str(fill.get('coin') or '')
        config=None
        snapshot:AccountSnapshot|None=None
        try:
            snapshot=await self.master_snapshot()
            configs=position_configs(snapshot.perp_state)
            config=configs.get(asset)
            if config is None:
                snapshot=await self.master_snapshot(force_refresh=True)
                config=position_configs(snapshot.perp_state).get(asset)
            equity=snapshot.account_value
        except Exception:
            equity=await self._persisted_master_equity()

        if config is None:
            await self._metric_incr('master_leverage_unavailable_count')
            log.warning('Master leverage unavailable for realtime fill; increasing exposure will wait for reconciliation',extra={'asset':asset})

        live_weights=self._live_master_weights(snapshot,equity,fill)
        cid=uuid.uuid4().hex
        async with SessionLocal() as db:
            event,jobs=await persist_master_fill_and_jobs(
                db,fill=fill,master_equity=equity,fencing_token=self.lease.token,
                correlation_id=cid,source_network=settings.master_network,
                master_leverage=config.leverage if config else None,
                master_is_cross=config.is_cross if config else None,
                master_live_weights=live_weights,
            )
            if not event: return
            for job in jobs:
                try: await publish_job(self.redis,db,job)
                except Exception: log.warning('Redis publish failed; durable job remains in PostgreSQL',extra={'job_id':str(job.id)},exc_info=True)
            await db.commit()
            await _set_checkpoint(db,int(event.event_ts.timestamp()*1000),event.exchange_event_id)
        try: await self.redis.publish(f'{settings.REALTIME_CHANNEL_PREFIX}:system',__import__('json').dumps({'type':'master_fill','asset':event.asset,'price':str(event.price),'size':str(event.size),'at':event.event_ts.isoformat(),'network':settings.master_network}))
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
