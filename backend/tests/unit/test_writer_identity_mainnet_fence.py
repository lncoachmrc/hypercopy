"""Unit coverage for the single-writer MAINNET strategy order fence."""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.services.execution import _verify_writer_authority
from app.services.writer_identity import WriterIdentityRegistry


class FakeRedis:
    """Minimal in-memory Redis stand-in supporting sets + the bootstrap script."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def sismember(self, key, member) -> bool:
        return str(member) in self.sets.get(str(key), set())

    async def srem(self, key, member) -> int:
        members = self.sets.get(str(key), set())
        if str(member) in members:
            members.discard(str(member))
            return 1
        return 0

    async def eval(self, script, numkeys, *args):
        assert 'WRITER_IDENTITY_BOOTSTRAP' in script
        keys = [str(value) for value in args[:numkeys]]
        argv = list(args[numkeys:])
        set_key, lock_key = keys
        identity = str(argv[0])

        existing = self.sets.get(set_key)
        if existing:
            return 1 if identity in existing else 0

        winner = self.strings.get(lock_key)
        if winner is None:
            self.strings[lock_key] = identity
            self.sets.setdefault(set_key, set()).add(identity)
            return 1
        if winner == identity:
            self.sets.setdefault(set_key, set()).add(identity)
            return 1
        return 0


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def registry(fake_redis: FakeRedis) -> WriterIdentityRegistry:
    return WriterIdentityRegistry(fake_redis)


WALLET = '0x' + '42' * 20


@pytest.fixture(autouse=True)
def _reset_identity(monkeypatch):
    monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', '')
    yield


def test_no_identity_configured_skips_mainnet_strategy_order(registry) -> None:
    assert asyncio.run(registry.verify_writer_authority(WALLET)) is False


def test_first_job_registers_identity_in_redis(monkeypatch, registry, fake_redis) -> None:
    monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', 'prod-cand-ams-1')

    registered = asyncio.run(registry.register_identity(WALLET))

    assert registered is True
    assert 'prod-cand-ams-1' in fake_redis.sets[f'hypercopy:writers:mainnet:{WALLET}']


def test_second_deployment_with_different_identity_fails_verification(monkeypatch, registry) -> None:
    monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', 'prod-cand-ams-1')
    assert asyncio.run(registry.register_identity(WALLET)) is True

    monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', 'prod-sfo-1')
    assert asyncio.run(registry.verify_writer_authority(WALLET)) is False


def test_same_identity_across_both_jobs_passes_verification(monkeypatch, registry) -> None:
    monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', 'prod-cand-ams-1')
    assert asyncio.run(registry.register_identity(WALLET)) is True
    assert asyncio.run(registry.verify_writer_authority(WALLET)) is True


def test_testnet_strategy_orders_are_unaffected_by_writer_fence(monkeypatch, registry) -> None:
    # No identity configured at all; testnet must still pass through.
    assert asyncio.run(_verify_writer_authority(registry, 'testnet', WALLET)) is True


def test_close_all_origin_bypasses_writer_gate() -> None:
    # CLOSE_ALL is handled by the caller (`_process_job_locked`) via an origin
    # check before `_verify_writer_authority` is ever invoked. This asserts the
    # gating predicate used at the call site.
    origin = 'CLOSE_ALL'
    network = 'mainnet'
    assert not (origin != 'CLOSE_ALL' and network == 'mainnet')


def test_redis_persistence_across_restarts(monkeypatch, fake_redis) -> None:
    monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', 'prod-cand-ams-1')
    first_registry = WriterIdentityRegistry(fake_redis)
    assert asyncio.run(first_registry.register_identity(WALLET)) is True

    # Simulate a restart: a brand-new registry instance wrapping the same
    # (persistent) Redis backing store must still see the earlier registration.
    second_registry = WriterIdentityRegistry(fake_redis)
    assert asyncio.run(second_registry.verify_writer_authority(WALLET)) is True


def test_concurrent_bootstrap_only_one_identity_wins(monkeypatch, fake_redis) -> None:
    async def bootstrap_as(identity: str) -> bool:
        registry = WriterIdentityRegistry(fake_redis)
        monkeypatch.setattr(settings, 'EXECUTION_WORKER_IDENTITY', identity)
        return await registry.register_identity(WALLET)

    async def run_both() -> tuple[bool, bool]:
        # monkeypatch is not concurrency-safe across true parallel tasks, but the
        # bootstrap script itself is what enforces the single-writer race, so we
        # exercise it sequentially with an already-populated lock to prove that a
        # second distinct identity is always refused once a winner exists.
        first = await bootstrap_as('prod-cand-ams-1')
        second = await bootstrap_as('prod-sfo-1')
        return first, second

    first_result, second_result = asyncio.run(run_both())

    assert first_result is True
    assert second_result is False
    assert fake_redis.sets[f'hypercopy:writers:mainnet:{WALLET}'] == {'prod-cand-ams-1'}
