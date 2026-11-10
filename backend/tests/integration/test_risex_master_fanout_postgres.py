"""Confirmed master EVENT fanout: RISEx-only ACTIVE testnet account selection.

Uses isolated TEMP tables on CI PostgreSQL 16; no live Railway or exchange I/O.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.session import engine
from app.services.copy import eligible_copy_followers


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1", reason="requires ephemeral CI PostgreSQL",
)


@pytest_asyncio.fixture
async def session():
    async with engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text(
                "CREATE TEMP TABLE users (id uuid, state text, copy_state text, "
                "execution_provider text, execution_network text, auth_wallet text, "
                "active_execution_epoch_id uuid) ON COMMIT DROP"
            ))
            await conn.execute(text(
                "CREATE TEMP TABLE trading_accounts (user_id uuid) ON COMMIT DROP"
            ))
            await conn.execute(text(
                "CREATE TEMP TABLE risex_trading_accounts (user_id uuid, account_address text) ON COMMIT DROP"
            ))
            await conn.execute(text(
                "CREATE TEMP TABLE execution_epochs (id uuid, user_id uuid, "
                "provider text, network text, account_address text, ended_at timestamptz) ON COMMIT DROP"
            ))
            factory=async_sessionmaker(conn,join_transaction_mode="create_savepoint",
                expire_on_commit=False)
            yield factory
        finally:
            await tx.rollback()
    await engine.dispose()


async def user_insert(db, *, provider, state="ACTIVE", copy="ACTIVE",
                      network="testnet", legacy=False, risex=False,
                      epoch_provider=None, epoch_network=None,
                      account_match=True, epoch_ended=False, auth=None):
    uid=uuid.uuid4()
    epoch_id=uuid.uuid4() if epoch_provider else None
    wallet=auth or "0x"+uid.hex[:40]
    address="0x"+uid.hex[:40]
    await db.execute(text("""
        INSERT INTO users
        (id,state,copy_state,execution_provider,execution_network,auth_wallet,active_execution_epoch_id)
        VALUES (:id,:state,:copy,:provider,:network,:auth,:epoch)
    """),{"id":uid,"state":state,"copy":copy,"provider":provider,
          "network":network,"auth":wallet,"epoch":epoch_id})
    if legacy:
        await db.execute(text("INSERT INTO trading_accounts(user_id) VALUES (:id)"),{"id":uid})
    if risex:
        await db.execute(text(
            "INSERT INTO risex_trading_accounts(user_id,account_address) VALUES (:id,:addr)"
        ),{"id":uid,"addr":address})
    if epoch_id:
        await db.execute(text("""
            INSERT INTO execution_epochs
            (id,user_id,provider,network,account_address,ended_at)
            VALUES (:id,:uid,:provider,:network,:address,:ended)
        """),{"id":epoch_id,"uid":uid,"provider":epoch_provider,
              "network":epoch_network or network,
              "address":address if account_match else "0x"+"ff"*20,
              "ended": datetime.now(UTC) if epoch_ended else None})
    return uid


@pytest.mark.asyncio
async def test_risex_only_active_testnet_followers_receive_master_events_only_in_isolated_stack(
    session,
):
    async with session() as db:
        included=await user_insert(db,provider="risex",risex=True,
            epoch_provider="risex",epoch_network="testnet",copy="ACTIVE")
        shadow=await user_insert(db,provider="risex",risex=True,
            epoch_provider="risex",copy="SHADOW")
        paused=await user_insert(db,provider="risex",risex=True,
            epoch_provider="risex",copy="PAUSED")
        wrong_network=await user_insert(db,provider="risex",risex=True,
            epoch_provider="risex",epoch_network="mainnet")
        mismatched=await user_insert(db,provider="risex",risex=True,
            epoch_provider="risex",account_match=False)
        expired=await user_insert(db,provider="risex",risex=True,
            epoch_provider="risex",epoch_ended=True)
        missing_account=await user_insert(db,provider="risex",risex=False,
            epoch_provider="risex")
        unbound=await user_insert(db,provider="risex",risex=True)
        with_legacy=await user_insert(db,provider="risex",risex=True,legacy=True,
            epoch_provider="risex",epoch_network="testnet")
        hl=await user_insert(db,provider="hyperliquid",legacy=True,
            epoch_provider="hyperliquid",epoch_network="mainnet",copy="SHADOW")
        old_hl=await user_insert(db,provider="hyperliquid",legacy=True)
        master_wallet="0x"+"aa"*20
        excluded_master=await user_insert(db,provider="hyperliquid",legacy=True,
            auth=master_wallet)
        await db.commit()

        isolated=await eligible_copy_followers(db,master_address=master_wallet,
            allow_risex_testnet=True)
        result={uid:network for uid,network in isolated}
        assert result=={
            included:"testnet",with_legacy:"testnet",
            hl:"mainnet",old_hl:"testnet",
        }
        assert len(result)==len(isolated)
        assert excluded_master not in result
        for prohibited in (shadow,paused,wrong_network,mismatched,expired,
                           missing_account,unbound):
            assert prohibited not in result

        live=await eligible_copy_followers(db,master_address=master_wallet,
            allow_risex_testnet=False)
        assert {uid:n for uid,n in live}=={
            hl:"mainnet",old_hl:"testnet",
        }


@pytest.mark.asyncio
async def test_suspended_risex_and_stale_legacy_provider_never_selected(session):
    async with session() as db:
        suspended=await user_insert(db,provider="risex",state="SUSPENDED",
            risex=True,epoch_provider="risex")
        legacy_risex_unbound=await user_insert(db,provider="risex",legacy=True)
        await db.commit()
        result=await eligible_copy_followers(db,master_address="",
            allow_risex_testnet=True)
        assert result==[]
        assert suspended not in [uid for uid,_ in result]
        assert legacy_risex_unbound not in [uid for uid,_ in result]
