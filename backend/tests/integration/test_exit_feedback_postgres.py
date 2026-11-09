"""Real PostgreSQL evidence matching; fixtures are temporary and rolled back."""
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.models.entities import AIProfitExitDecision, AIProfitExitObservation
from app.services.ai_exit_feedback import FEEDBACK_VERSION, read_exit_feedback
from app.services.risex_profit_exit_shadow import risex_shadow_candidates

pytestmark = pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='requires CI PostgreSQL')


@pytest_asyncio.fixture
async def database():
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            for table in ('ai_profit_exit_decisions', 'ai_profit_exit_observations'):
                await connection.execute(text(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL) ON COMMIT DROP'))
            yield async_sessionmaker(connection, expire_on_commit=False, join_transaction_mode='create_savepoint')
        finally:
            await transaction.rollback()
    await engine.dispose()


def records(now):
    scope=dict(user_id=uuid.UUID(int=101), execution_epoch_id=uuid.UUID(int=102),
               execution_provider='risex', execution_network='testnet', asset='BTC',
               source_cycle_id='testnet:BTC:old', state_version=123, pnl_complete=False)
    inputs=dict(decision_mode='SHADOW', learning_policy_version=FEEDBACK_VERSION,
                entry_price='100', evaluation_basis='risex_copy_shadow_estimate',
                funding_model='not_modeled_fee_1E+1bps')
    decision=AIProfitExitDecision(**scope, id=uuid.uuid4(), side='SHORT',
        source_cycle_open_event_id=uuid.uuid4(), source_master_position=D('-1'),
        follower_position_size=D('-1'), action='HOLD', intent_state=None, copy_job_id=None,
        net_pnl=D('5'), position_verified_at=now-timedelta(hours=2),
        decision_inputs=inputs, decision_reason='fixture', model_provider='fixture',model_name='fixture',
        decided_at=now-timedelta(hours=2), expires_at=now)
    observation=AIProfitExitObservation(**scope, id=uuid.uuid4(), evaluation_slot=1,
        follower_position_size=D('-1'), evaluation_basis=inputs['evaluation_basis'],
        funding_model=inputs['funding_model'], mark_price=D('98'), executable_exit_price=D('98'),
        entry_price=D('100'), net_pnl=D('-2'), complete=True, eligible=False, position_matches=True,
        reason='fixture', observed_at=now-timedelta(minutes=90))
    return decision,observation


async def read(db,now):
    return await read_exit_feedback(db, user_id=uuid.UUID(int=101),epoch_id=uuid.UUID(int=102),
        provider='risex',network='testnet',asset='BTC',side='SHORT',
        basis='risex_copy_shadow_estimate',pnl_complete=False,funding_model='not_modeled_fee_1E+1bps',
        current_cycle='testnet:BTC:current',as_of=now)


@pytest.mark.asyncio
async def test_mature_losses_reach_feedback_and_duplicate_decisions_do_not_inflate(database):
    now=datetime.now(UTC)
    async with database() as db:
        decision,observation=records(now)
        duplicate,_=records(now)
        duplicate.decided_at+=timedelta(minutes=1)
        duplicate.net_pnl=D('100')
        db.add_all([decision,duplicate,observation])
        await db.commit()
        summary=await read(db,now)
        assert summary['sample_count']==1
        assert D(summary['mean_close_advantage_bps'])==D('700')
        assert summary['realized_performance'] is False
        assert summary['promotion_allowed'] is False


@pytest.mark.parametrize('field,value',[
    ('execution_provider','hyperliquid'),('execution_network','mainnet'),
    ('execution_epoch_id',uuid.UUID(int=222)),('user_id',uuid.UUID(int=333)),
    ('source_cycle_id','testnet:BTC:other'),('state_version',999),
    ('entry_price',D('99')),('funding_model','not_modeled_fee_5bps'),
])
@pytest.mark.asyncio
async def test_other_identity_or_cost_basis_is_never_used(database,field,value):
    now=datetime.now(UTC)
    async with database() as db:
        d,o=records(now)
        setattr(o,field,value)
        db.add_all([d,o]);await db.commit()
        assert (await read(db,now))['sample_count']==0


@pytest.mark.asyncio
async def test_database_future_observation_cannot_enter_feedback(database):
    now=datetime.now(UTC)
    async with database() as db:
        d,o=records(now)
        d.decided_at=now-timedelta(minutes=29)
        o.observed_at=now+timedelta(minutes=1)
        db.add_all([d,o]);await db.commit()
        assert (await read(db,now))['sample_count']==0


@pytest.mark.asyncio
async def test_risex_candidate_statement_executes_without_legacy_hl_account_join(database):
    async with database() as db:
        stmt=risex_shadow_candidates()
        sql=str(stmt)
        assert 'risex_trading_accounts' in sql
        assert 'JOIN trading_accounts ' not in sql
        # Real ORM SQL is parsed/executed against migrated PostgreSQL.
        await db.execute(stmt)
