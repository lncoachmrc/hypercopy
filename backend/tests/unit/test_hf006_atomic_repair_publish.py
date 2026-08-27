from __future__ import annotations

import time

import pytest

from app.core.config import settings
from app.services.master_leverage_cache import (
    master_leverage_metric_snapshot,
    publish_master_leverage_repair,
    record_master_leverage_missing,
)


class AtomicPublishRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.streams: dict[str, list[dict[str, str]]] = {}
        self.now = time.time()
        self.eval_calls = 0

    async def time(self):
        seconds = int(self.now)
        micros = int((self.now - seconds) * 1_000_000)
        return seconds, micros

    async def get(self, key):
        return self.values.get(str(key))

    async def incr(self, key):
        name = str(key)
        value = int(float(self.values.get(name, '0'))) + 1
        self.values[name] = str(value)
        return value

    async def zrange(self, key, start, stop, withscores=False):
        ordered = sorted(self.zsets.get(str(key), {}).items(), key=lambda item: (item[1], item[0]))
        selected = ordered[start:] if stop == -1 else ordered[start:stop + 1]
        return selected if withscores else [member for member, _ in selected]

    async def zrem(self, key, *members):
        values = self.zsets.setdefault(str(key), {})
        return sum(1 for member in members if values.pop(str(member), None) is not None)

    async def xadd(self, *_args, **_kwargs):
        raise AssertionError('repair publication must not call XADD outside the atomic Lua script')

    def _metric_incr(self, key: str) -> int:
        value = int(float(self.values.get(key, '0'))) + 1
        self.values[key] = str(value)
        return value

    async def eval(self, script, numkeys, *args):
        self.eval_calls += 1
        keys = [str(value) for value in args[:numkeys]]
        argv = list(args[numkeys:])

        if 'HF006_REGISTER_MISSING' in script:
            assert numkeys == 4
            started = self.zsets.setdefault(keys[0], {})
            latest = self.zsets.setdefault(keys[1], {})
            repairs = self.zsets.setdefault(keys[2], {})
            member = str(argv[0])
            intent_order = float(argv[1])
            repaired = repairs.get(member)
            if repaired is not None and repaired >= intent_order:
                return 0
            created = 0
            if member not in started:
                started[member] = float(argv[2]) if argv[2] != '' else self.now
                self._metric_incr(keys[3])
                created = 1
            latest[member] = max(latest.get(member, 0.0), intent_order)
            return created

        if 'HF006_PUBLISH_REPAIR' in script:
            assert numkeys == 8
            stream = self.streams.setdefault(keys[0], [])
            started = self.zsets.setdefault(keys[1], {})
            latest = self.zsets.setdefault(keys[2], {})
            repairs = self.zsets.setdefault(keys[3], {})
            member = str(argv[0])
            evidence_order = float(argv[1])
            slo_seconds = float(argv[2])
            job_id = str(argv[3])
            message_id = f'{len(stream) + 1}-0'
            stream.append({'id': message_id, 'job_id': job_id})
            repairs[member] = max(repairs.get(member, 0.0), evidence_order)

            started_at = started.get(member)
            latest_order = latest.get(member)
            if started_at is None or latest_order is None or evidence_order < latest_order:
                return message_id

            current = float(argv[5]) if argv[5] != '' else self.now
            duration = max(current - started_at, 0.0)
            del started[member]
            del latest[member]
            self._metric_incr(keys[4])
            self.values[keys[5]] = f'{duration:.6f}'
            previous_max = float(self.values.get(keys[6], '0'))
            if duration > previous_max:
                self.values[keys[6]] = f'{duration:.6f}'
            if duration > slo_seconds:
                self._metric_incr(keys[7])
            return message_id

        raise AssertionError('unexpected Lua script')


@pytest.fixture
def configured_master(monkeypatch):
    monkeypatch.setattr(settings, 'HYPERLIQUID_MASTER_ADDRESS', '0xabc')
    monkeypatch.setattr(settings, 'MASTER_LEVERAGE_RECOVERY_SLO_SECONDS', 60.0)


@pytest.mark.asyncio
async def test_repair_stream_entry_and_recovery_accounting_share_one_lua(configured_master):
    redis = AtomicPublishRedis()
    await record_master_leverage_missing(
        redis,
        'user-1',
        'BTC',
        intent_order=10,
        now=100.0,
    )
    before_publish_calls = redis.eval_calls

    message_id = await publish_master_leverage_repair(
        redis,
        stream_name='hypercopy:test-stream',
        job_id='job-1',
        user_id='user-1',
        asset='BTC',
        evidence_order=20,
        now=165.0,
    )
    metrics = await master_leverage_metric_snapshot(redis, now=165.0)

    assert redis.eval_calls == before_publish_calls + 1
    assert message_id == '1-0'
    assert redis.streams['hypercopy:test-stream'] == [{'id': '1-0', 'job_id': 'job-1'}]
    assert metrics['master_leverage_missing_active_count'] == 0
    assert metrics['master_leverage_recovery_count'] == 1
    assert metrics['master_leverage_recovery_last_seconds'] == pytest.approx(65.0)
    assert metrics['master_leverage_recovery_slo_breach_count'] == 1


@pytest.mark.asyncio
async def test_republishing_after_lost_reply_does_not_double_count_recovery(configured_master):
    redis = AtomicPublishRedis()
    await record_master_leverage_missing(
        redis,
        'user-1',
        'ETH',
        intent_order=10,
        now=100.0,
    )

    await publish_master_leverage_repair(
        redis,
        stream_name='hypercopy:test-stream',
        job_id='job-2',
        user_id='user-1',
        asset='ETH',
        evidence_order=20,
        now=165.0,
    )
    await publish_master_leverage_repair(
        redis,
        stream_name='hypercopy:test-stream',
        job_id='job-2',
        user_id='user-1',
        asset='ETH',
        evidence_order=20,
        now=166.0,
    )
    metrics = await master_leverage_metric_snapshot(redis, now=166.0)

    assert len(redis.streams['hypercopy:test-stream']) == 2
    assert metrics['master_leverage_missing_active_count'] == 0
    assert metrics['master_leverage_recovery_count'] == 1
    assert metrics['master_leverage_recovery_slo_breach_count'] == 1
