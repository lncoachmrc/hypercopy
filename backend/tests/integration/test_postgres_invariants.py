"""Integration invariants. Run in CI after `alembic upgrade head`.

These tests are intentionally small; end-to-end testnet and chaos tests remain a
release gate, not something faked in an isolated GitHub runner.
"""
import os
import uuid

import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db.session import SessionLocal, engine

pytestmark=pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',reason='requires CI PostgreSQL')


@pytest_asyncio.fixture(autouse=True)
async def _dispose_pool_after_test():
    """Do not carry asyncpg connections across pytest's per-test event loops."""
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_expected_schema_and_safety_flags_exist():
    async with SessionLocal() as db:
        rev=(await db.execute(text('select version_num from alembic_version'))).scalar_one()
        expected=ScriptDirectory.from_config(Config('backend/alembic.ini')).get_current_head()
        assert rev==expected,f'Database migration {rev} does not match current head {expected}'
        flags=(await db.execute(text("select slug from system_flags where slug in ('live_trading','global_pause','emergency_stop')"))).scalars().all()
        assert set(flags)=={'live_trading','global_pause','emergency_stop'}


@pytest.mark.asyncio
async def test_master_event_unique_constraint():
    eid='ci:'+uuid.uuid4().hex
    async with SessionLocal() as db:
        await db.execute(text("insert into master_events(exchange_event_id,asset,side,size,price,start_position,position_after,master_equity,event_ts,raw,fencing_token,id) values(:e,'BTC','B',1,1,[...]
        await db.commit()
        with pytest.raises(Exception):
            await db.execute(text("insert into master_events(exchange_event_id,asset,side,size,price,start_position,position_after,master_equity,event_ts,raw,fencing_token,id) values(:e,'BTC','B',[...]
            await db.commit()
        await db.rollback()
