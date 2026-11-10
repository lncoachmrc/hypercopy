"""Real source-cycle sizing reused for RISEx SHADOW, never CopyJob writes."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from app.services import ai_profit_exit_risex_targets as target


class DummyDB:
    def __init__(self, rows=(), epoch=None):
        self.rows=list(rows)
        self.epoch=epoch
        self.calls=[]
    async def execute(self, stmt):
        self.calls.append("query")
        return NS(all=lambda: self.rows)
    async def get(self,cls,key):
        self.calls.append("epoch")
        return self.epoch
    async def rollback(self):
        self.calls.append("rollback")


class DummyHL:
    def __init__(self, equity="1000"):
        self.requests=0
        self.snapshot=NS(
            account_value=D(equity),
            perp_state={"assetPositions":[{"position":{
                "coin":"BTC","szi":"-1","entryPx":"100",
            }}]},
        )
    async def mids(self,*,priority):
        self.requests+=1
        return {"BTC":"100"}


def _user_and_account():
    uid=uuid.uuid4()
    return (NS(
        id=uid,active_execution_epoch_id=uuid.uuid4(),
        execution_provider="risex",
    ), NS(user_id=uid,account_address="0x"+"aa"*20))


def _epoch(user,account):
    return NS(id=user.active_execution_epoch_id,user_id=user.id,
              provider="risex",network="testnet",
              account_address=account.account_address,
              ended_at=None,started_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_live_or_malformed_environment_never_reads_provider_or_database(monkeypatch):
    for raw in ["true","FALSE","",None]:
        if raw is None:
            monkeypatch.delenv("ENABLE_LIVE_TRADING",raising=False)
        else:
            monkeypatch.setenv("ENABLE_LIVE_TRADING",raw)
        db=DummyDB()
        hl=DummyHL()
        result=await target.refresh_risex_shadow_targets(
            db,master_hl=hl,master_snapshot=hl.snapshot,
            snapshot_started_order=10,
        )
        assert result=={"reviewed":0,"updated":0,"skipped":0}
        assert not db.calls
        assert hl.requests==0


@pytest.mark.asyncio
async def test_no_follower_is_a_noop(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING","false")
    db=DummyDB()
    hl=DummyHL()
    result=await target.refresh_risex_shadow_targets(
        db,master_hl=hl,master_snapshot=hl.snapshot,snapshot_started_order=10,
    )
    assert result["updated"]==0
    assert hl.requests==0


@pytest.mark.asyncio
async def test_legacy_or_wrong_epoch_cannot_refresh_risex_target(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING","false")
    user, account=_user_and_account()
    db=DummyDB(rows=[(user,account)],epoch=_epoch(user,account))
    db.epoch.account_address="0x"+"bb"*20
    hl=DummyHL()
    async def forbidden(*args,**kwargs):
        raise AssertionError("RISEx read must not occur")
    monkeypatch.setattr(target,"read_risex_reconcile_observation",forbidden)
    outcome=await target.refresh_risex_shadow_targets(
        db,master_hl=hl,master_snapshot=hl.snapshot,
        snapshot_started_order=10,
    )
    assert outcome["updated"]==0
    assert outcome["skipped"]==1


@pytest.mark.asyncio
async def test_risex_shadow_reuses_observed_reconcile_without_jobs(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING","false")
    user, account=_user_and_account()
    epoch=_epoch(user,account)
    db=DummyDB(rows=[(user,account)],epoch=epoch)
    hl=DummyHL()
    recorded=[]
    @asynccontextmanager
    async def dummy_lock(user_id):
        assert user_id==user.id
        yield
    async def read(**kwargs):
        recorded.append(("read",kwargs))
        return NS(provider="risex",network="testnet",epoch_id=epoch.id)
    async def reconcile(_db,_user,**kwargs):
        recorded.append(("reconcile",kwargs))
        return {"status":"OK","jobs_created":0}
    monkeypatch.setattr(target,"position_ledger_lock",dummy_lock)
    monkeypatch.setattr(target,"read_risex_reconcile_observation",read)
    monkeypatch.setattr(target,"reconcile_observed_follower",reconcile)
    monkeypatch.setattr(target,"position_configs",lambda _: {})
    result=await target.refresh_risex_shadow_targets(
        db,master_hl=hl,master_snapshot=hl.snapshot,
        snapshot_started_order=101,
    )
    assert result=={"reviewed":1,"updated":1,"skipped":0}
    assert hl.requests==1
    assert recorded[0][0]=="read"
    assert recorded[1][0]=="reconcile"
    assert recorded[1][1]["create_jobs"] is False
    assert recorded[1][1]["master_positions"]["BTC"]==D("-1")
    assert recorded[1][1]["master_mids"].snapshot_started_order==101


@pytest.mark.asyncio
async def test_indeterminate_master_cannot_generate_synthetic_targets(monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_TRADING","false")
    user,account=_user_and_account()
    db=DummyDB(rows=[(user,account)],epoch=_epoch(user,account))
    hl=DummyHL(equity="0")
    result=await target.refresh_risex_shadow_targets(
        db,master_hl=hl,master_snapshot=hl.snapshot,
        snapshot_started_order=10,
    )
    assert result["updated"]==0
    assert hl.requests==0
