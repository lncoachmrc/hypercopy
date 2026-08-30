from __future__ import annotations

import inspect
import uuid
from decimal import Decimal

import pytest

from app.adapters.hyperliquid import HyperliquidAdapter
from app.models.entities import CopyJob, JobState
from app.services import copy as copy_service
from app.services import queue as queue_service
from app.services.strategy_intents import (
    StrategyIntentAuthorizationError,
    _job_evidence,
    master_position_from_state,
)


def _job(context: dict, *, origin: str = 'EVENT') -> CopyJob:
    return CopyJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        asset='BTC',
        origin=origin,
        state=JobState.QUEUED,
        correlation_id=uuid.uuid4().hex,
        context=context,
    )


def test_strategy_intent_evidence_prefers_unscaled_source_master_position() -> None:
    job = _job({
        'master_intent_order': 42,
        'follower_network': 'mainnet',
        'master_position': '0.7',
        'source_master_position': '1',
    }, origin='RECONCILE')

    evidence = _job_evidence(job)

    assert evidence is not None
    assert evidence.intent_order == 42
    assert evidence.source_master_position == Decimal('1')
    assert evidence.follower_network == 'mainnet'


def test_unversioned_strategy_intent_fails_closed() -> None:
    job = _job({
        'follower_network': 'mainnet',
        'master_position': '1',
    })

    with pytest.raises(StrategyIntentAuthorizationError, match='unversioned'):
        _job_evidence(job)


def test_non_strategy_action_is_outside_latest_intent_fence() -> None:
    job = _job({}, origin='CLOSE_ALL')
    assert _job_evidence(job) is None


def test_master_position_from_state_reads_signed_size_and_defaults_flat() -> None:
    state = {
        'assetPositions': [
            {'position': {'coin': 'ETH', 'szi': '-2.5'}},
            {'position': {'coin': 'BTC', 'szi': '0.125'}},
        ]
    }
    assert master_position_from_state(state, 'BTC') == Decimal('0.125')
    assert master_position_from_state(state, 'SOL') == Decimal('0')


def test_copy_order_has_final_signed_action_authorization_fence() -> None:
    source = inspect.getsource(HyperliquidAdapter.place_ioc)
    assert 'current_strategy_intent_for_cloid(' in source
    assert 'priority=Priority.ORDER' in source
    assert 'master_position_from_state(' in source
    assert 'fresh_position != evidence.source_master_position' in source
    assert 'before_submit=_authorize_strategy_order' in source
    assert 'except StrategyIntentAuthorizationError as exc:' in source
    assert "'CANCELED'" in source


def test_paused_followers_do_not_receive_realtime_event_fanout() -> None:
    source = inspect.getsource(copy_service.persist_master_fill_and_jobs)
    assert "u.copy_state IN ('ACTIVE', 'SHADOW')" in source
    assert "u.state = 'ACTIVE'" in source


def test_queue_coalesces_strategy_intents_before_any_publish_path() -> None:
    publish_source = inspect.getsource(queue_service.publish_job)
    repair_source = inspect.getsource(queue_service.repair_stream)
    assert 'prepare_strategy_job_for_publish(db, job)' in publish_source
    assert 'prepare_strategy_job_for_publish(db, job)' in repair_source
