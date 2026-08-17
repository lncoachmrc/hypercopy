from __future__ import annotations

import asyncio
import json
import math
import os
import signal
from datetime import UTC, datetime

from sqlalchemy import select, text

from app.core.logging import configure_logging, get_logger
from app.db.redis import redis_client
from app.db.schema import assert_schema
from app.db.session import SessionLocal, engine
from app.models.entities import MasterEvent
from app.services.ai_intelligence import read_ai_intelligence, refresh_ai_intelligence

configure_logging(); log=get_logger(__name__)
stop=asyncio.Event()


def _stop(*_):
    stop.set()


def _env_bool(name: str, default: bool = False) -> bool:
    value=os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1','true','yes','on'}


def _queue_name() -> str:
    return os.getenv('LLM_EVENT_QUEUE','hypercopy:ai:master-events').strip() or 'hypercopy:ai:master-events'


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if dt.tzinfo is None:
            dt=dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None


def _signal_ts(payload: str) -> datetime | None:
    try:
        value=json.loads(payload)
        return _parse_ts(value.get('event_ts')) if isinstance(value,dict) else None
    except Exception:
        return None


class AIIntelligenceWorker:
    def __init__(self):
        self.redis=redis_client()
        self.queue=_queue_name()
        self.debounce=max(int(os.getenv('LLM_EVENT_DEBOUNCE_SECONDS','180')),0)
        self.min_refresh=max(int(os.getenv('LLM_MIN_REFRESH_SECONDS','300')),60)
        self.max_refresh=max(int(os.getenv('LLM_MAX_REFRESH_SECONDS','21600')),self.min_refresh)
        self.failure_retry=max(int(os.getenv('LLM_FAILURE_RETRY_SECONDS','1800')),self.min_refresh)
        self.poll=max(min(int(os.getenv('LLM_EVENT_POLL_SECONDS','10')),15),1)
        self.db_poll=max(int(os.getenv('LLM_DB_FALLBACK_POLL_SECONDS','60')),self.poll)
        self._last_db_poll=0.0
        self._pending=False
        self._last_signal_mono: float | None=None
        self._source_ts: datetime | None=None

    async def _latest_master_event(self) -> MasterEvent | None:
        async with SessionLocal() as db:
            return (await db.execute(
                select(MasterEvent).order_by(MasterEvent.event_ts.desc()).limit(1)
            )).scalar_one_or_none()

    async def _state(self) -> dict:
        async with SessionLocal() as db:
            return await read_ai_intelligence(db)

    async def _sync_source_from_state(self) -> dict:
        state=await self._state()
        self._source_ts=_parse_ts(state.get('source_event_ts'))
        return state

    async def _has_unanalysed_events(self) -> bool:
        latest=await self._latest_master_event()
        if latest is None:
            return False
        return self._source_ts is None or latest.event_ts > self._source_ts

    @staticmethod
    def _state_age_seconds(state: dict) -> float:
        updated=_parse_ts(state.get('updated_at'))
        if updated is None:
            return float('inf')
        return max((datetime.now(UTC)-updated).total_seconds(),0.0)

    async def _run_analysis(self, reason: str) -> bool:
        """Run one AI analysis under an AI-only singleton advisory lock.

        This lock is completely separate from the reconciliation lock. Holding it
        during provider failover cannot delay master fill ingestion, copy jobs,
        risk checks, reconciliation or Hyperliquid execution.
        """
        async with engine.connect() as conn:
            acquired=bool((await conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext('hypercopy:ai-intelligence'))")
            )).scalar_one())
            if not acquired:
                log.info('AI intelligence analysis skipped; another AI worker owns the lock')
                return False
            try:
                async with SessionLocal() as db:
                    state=await refresh_ai_intelligence(db,force=True)
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(hashtext('hypercopy:ai-intelligence'))"))
                await conn.commit()

        self._source_ts=_parse_ts(state.get('source_event_ts'))
        log.info('AI intelligence analysis completed',extra={
            'reason':reason,
            'status':state.get('status'),
            'provider':state.get('provider'),
            'model':state.get('model'),
            'fallback_index':state.get('fallback_index'),
            'source_event_ts':state.get('source_event_ts'),
        })
        return True

    async def _consume_signal(self) -> bool:
        try:
            result=await self.redis.brpop(self.queue,timeout=self.poll)
        except Exception:
            log.warning('AI event queue read failed; PostgreSQL fallback remains active',exc_info=True)
            await asyncio.sleep(min(self.poll,5))
            return False
        if not result:
            return False

        _,payload=result
        ts=_signal_ts(payload)
        if ts is not None and self._source_ts is not None and ts <= self._source_ts:
            return False

        self._pending=True
        self._last_signal_mono=asyncio.get_running_loop().time()

        # Collapse an event burst into one future analysis. New signals that
        # arrive while the LLM is running remain queued and are handled after it.
        try:
            while True:
                payload=await self.redis.rpop(self.queue)
                if payload is None:
                    break
                ts=_signal_ts(payload)
                if ts is None or self._source_ts is None or ts > self._source_ts:
                    self._pending=True
                    self._last_signal_mono=asyncio.get_running_loop().time()
        except Exception:
            log.warning('AI queue burst drain failed; continuing with current pending batch',exc_info=True)
        return True

    async def _db_fallback_check(self):
        now=asyncio.get_running_loop().time()
        if now-self._last_db_poll < self.db_poll:
            return
        self._last_db_poll=now
        if await self._has_unanalysed_events():
            self._pending=True
            # The DB event may already be older than the debounce window. Analyze
            # immediately once min-refresh allows instead of waiting another 3m.
            self._last_signal_mono=now-self.debounce

    async def run(self):
        async with SessionLocal() as db:
            await assert_schema(db)

        log.info('AI intelligence worker started',extra={
            'queue':self.queue,
            'debounce_seconds':self.debounce,
            'min_refresh_seconds':self.min_refresh,
            'max_refresh_seconds':self.max_refresh,
        })

        if not _env_bool('LLM_ENABLED',False):
            log.info('AI intelligence worker idle because LLM_ENABLED=false')
            while not stop.is_set():
                await asyncio.sleep(30)
            return

        state=await self._sync_source_from_state()
        if await self._has_unanalysed_events():
            self._pending=True
            self._last_signal_mono=asyncio.get_running_loop().time()-self.debounce

        while not stop.is_set():
            await self._consume_signal()
            await self._db_fallback_check()

            now=asyncio.get_running_loop().time()
            state=await self._state()
            age=self._state_age_seconds(state)
            retry_after=self.failure_retry if state.get('status')=='degraded' else self.min_refresh

            if self._pending and self._last_signal_mono is not None:
                quiet_for=now-self._last_signal_mono
                if quiet_for >= self.debounce and age >= retry_after:
                    ran=await self._run_analysis('master_event_batch')
                    if ran:
                        # An event may have landed while provider calls were in
                        # flight. Compare against PostgreSQL before clearing.
                        self._pending=await self._has_unanalysed_events()
                        self._last_signal_mono=asyncio.get_running_loop().time() if self._pending else None
                    continue

            if not self._pending and age >= self.max_refresh:
                await self._run_analysis('safety_refresh')


async def main():
    loop=asyncio.get_running_loop()
    for s in (signal.SIGTERM,signal.SIGINT):
        try: loop.add_signal_handler(s,_stop)
        except NotImplementedError: pass
    await AIIntelligenceWorker().run()


if __name__=='__main__':
    asyncio.run(main())
