"""Integration invariants. Run in CI after `alembic upgrade head`.

These tests are intentionally small; end-to-end testnet and chaos tests remain a
release gate, not something faked in an isolated GitHub runner.
"""
import os
import uuid

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal

pytestmark=pytest.mark.skipif(os.getenv('RUN_INTEGRATION')!='1',reason='requires CI PostgreSQL')


@pytest.mark.asyncio
async def test_expected_schema_and_safety_flags_exist():
    async with SessionLocal() as db:
        rev=(await db.execute(text('select version_num from alembic_version'))).scalar_one()
        assert rev=='0004_ledger_marks'
        flags=(await db.execute(text("select slug from system_flags where slug in ('live_trading','global_pause','emergency_stop')"))).scalars().all()
        assert set(flags)=={'live_trading','global_pause','emergency_stop'}


@pytest.mark.asyncio
async def test_master_event_unique_constraint():
    eid='ci:'+uuid.uuid4().hex
    async with SessionLocal() as db:
        await db.execute(text("insert into master_events(exchange_event_id,asset,side,size,price,start_position,position_after,master_equity,event_ts,raw,fencing_token,id) values(:e,'BTC','B',1,1,0,1,100,now(),'{}',1,:id)"),{'e':eid,'id':uuid.uuid4()})
        await db.commit()
        with pytest.raises(Exception):
            await db.execute(text("insert into master_events(exchange_event_id,asset,side,size,price,start_position,position_after,master_equity,event_ts,raw,fencing_token,id) values(:e,'BTC','B',1,1,0,1,100,now(),'{}',1,:id)"),{'e':eid,'id':uuid.uuid4()})
            await db.commit()
        await db.rollback()
