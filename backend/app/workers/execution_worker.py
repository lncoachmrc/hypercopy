from __future__ import annotations

import asyncio
import signal
import uuid

from sqlalchemy import select, text

from app.adapters.hyperliquid import HyperliquidAdapter
from app.adapters.ratelimit import Budget, WeightedRateLimiter
from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.lease import replica_identity
from app.db.redis import redis_client
from app.db.session import SessionLocal, engine
from app.db.schema import assert_schema
from app.models.entities import CopyJob, JobState, User, WorkerHeartbeat
from app.services.execution import claim_job, process_job, release_stale_jobs
from app.services.credentials import monitor_credential_expiry
from app.services.queue import ensure_group, repair_stream
from app.services.reconcile import master_snapshot, reconcile_active_users, reconcile_user

configure_logging(); log=get_logger(__name__); stop=asyncio.Event()


def _stop(*_): stop.set()


def _job_matches_current_networks(job: CopyJob) -> bool:
    if job.origin not in {'EVENT', 'RECONCILE'}:
        return True
    ctx = job.context or {}
    return (
        ctx.get('master_network') == settings.master_network
        and ctx.get('follower_network') == settings.follower_network
    )


class Worker:
    def __init__(self):
        self.id = replica_identity()
        self.redis = redis_client()
        self.limiter = WeightedRateLimiter(self.redis, Budget(total_per_minute=settings.HL_RATE_BUDGET_PER_MIN))
        self.master_hl = HyperliquidAdapter(self.limiter, network=settings.master_network)
        self.follower_hl = HyperliquidAdapter(self.limiter, network=settings.follower_network)
        self.current_job = None

    async def heartbeat(self):
        async with SessionLocal() as db:
            hb=await db.get(WorkerHeartbeat,self.id)
            if not hb: hb=WorkerHeartbeat(worker_id=self.id,service='execution-worker'); db.add(hb)
            hb.seen_at=__import__('datetime').datetime.now(__import__('datetime').UTC); hb.current_job_id=self.current_job; await db.commit()

    async def handle_job_id(self,job_id:str)->bool:
        try: uid=uuid.UUID(job_id)
        except Exception: return True
        async with SessionLocal() as db:
            raw=await db.get(CopyJob,uid)
            if not raw or raw.state in {JobState.DONE,JobState.SKIPPED,JobState.DEAD}: return True

            # Historical jobs created before a network split (or under a
            # different source/destination pair) must never update targets or
            # submit orders in the current topology.
            if not _job_matches_current_networks(raw):
                raw.state=JobState.SKIPPED
                raw.last_error='Stale job from a different or unversioned Hyperliquid network topology'
                raw.owner=None
                raw.locked_until=None
                await db.commit()
                return True

            if raw.origin=='ADMIN_RECONCILE':
                user=await db.get(User,raw.user_id)
                if user:
                    mp,me,master_mids=await master_snapshot(self.master_hl)
                    follower_mids=master_mids if settings.master_network == settings.follower_network else await self.follower_hl.mids()
                    await reconcile_user(
                        db,self.follower_hl,user,
                        master_positions=mp,master_equity=me,mids=follower_mids,master_mids=master_mids,
                    )
                    await repair_stream(self.redis,db)
                raw.state=JobState.DONE; await db.commit(); return True
            job=await claim_job(db,self.id,uid)
            if not job: return True
            self.current_job=job.id; await self.heartbeat()
            result=await process_job(db,self.follower_hl,job)
            self.current_job=None
            try:
                await self.redis.publish(
                    f'{settings.REALTIME_CHANNEL_PREFIX}:user:{job.user_id}',
                    __import__('json').dumps({'type':'copy_job','job_id':str(job.id),'asset':job.asset,'state':result}),
                )
            except Exception:
                pass
            return result in {JobState.DONE.value,JobState.SKIPPED.value,JobState.DEAD.value,JobState.RETRYING.value}

    async def consume(self):
        await ensure_group(self.redis)
        while not stop.is_set():
            try:
                messages=await self.redis.xreadgroup(settings.STREAM_GROUP,self.id,{settings.STREAM_NAME:'>'},count=1,block=2000)
                if not messages:
                    await self.heartbeat(); continue
                for _,entries in messages:
                    for message_id,data in entries:
                        ack=await self.handle_job_id(data.get('job_id',''))
                        if ack: await self.redis.xack(settings.STREAM_NAME,settings.STREAM_GROUP,message_id)
            except Exception: log.exception('Worker consume loop failed'); await asyncio.sleep(2)

    async def maintenance(self):
        while not stop.is_set():
            try:
                async with SessionLocal() as db:
                    await release_stale_jobs(db)
                    await repair_stream(self.redis,db)
                    await monitor_credential_expiry(db, self.redis)

                await self.run_reconcile_if_leader()

                async with SessionLocal() as db:
                    await repair_stream(self.redis,db)
                await self.heartbeat()
            except Exception: log.warning('Worker maintenance failed',exc_info=True)
            await asyncio.sleep(settings.RECONCILE_INTERVAL_SECONDS)

    async def run_reconcile_if_leader(self):
        async with engine.connect() as conn:
            acquired=bool((await conn.execute(text("SELECT pg_try_advisory_lock(hashtext('hypercopy:reconciler'))"))).scalar_one())
            if not acquired: return
            try:
                async with SessionLocal() as db:
                    await reconcile_active_users(db,self.follower_hl,master_hl=self.master_hl)
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(hashtext('hypercopy:reconciler'))")); await conn.commit()

    async def run(self):
        async with SessionLocal() as db: await assert_schema(db)
        log.info('Execution worker networks', extra={'master_network': settings.master_network, 'follower_network': settings.follower_network})
        consume=asyncio.create_task(self.consume()); maintenance=asyncio.create_task(self.maintenance())
        await stop.wait()
        maintenance.cancel()
        await asyncio.gather(maintenance, return_exceptions=True)
        try:
            await asyncio.wait_for(consume, timeout=55)
        except asyncio.TimeoutError:
            consume.cancel()
            await asyncio.gather(consume, return_exceptions=True)


async def main():
    loop=asyncio.get_running_loop()
    for s in (signal.SIGTERM,signal.SIGINT):
        try: loop.add_signal_handler(s,_stop)
        except NotImplementedError: pass
    await Worker().run()

if __name__=='__main__': asyncio.run(main())
