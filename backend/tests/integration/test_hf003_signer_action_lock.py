"""PostgreSQL multi-process regression for HF-003 signer nonce coordination."""

import asyncio
import os
import sys

import pytest
import pytest_asyncio

from app.db.signer_action_lock import signer_action_lock, signer_action_lock_engine, signer_lock_id

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)

_CHILD = r'''
import asyncio
import sys
from app.db.signer_action_lock import signer_action_lock, signer_action_lock_engine

async def main():
    signer = sys.argv[1]
    hold_seconds = float(sys.argv[2])
    async with signer_action_lock(signer):
        print("LOCKED", flush=True)
        await asyncio.sleep(hold_seconds)
    await signer_action_lock_engine.dispose()

asyncio.run(main())
'''


@pytest_asyncio.fixture(autouse=True)
async def _dispose_lock_pool_after_test():
    yield
    await signer_action_lock_engine.dispose()


async def _start_lock_holder(signer: str, hold_seconds: float = 0.8):
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD,
        signer,
        str(hold_seconds),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    line = await asyncio.wait_for(proc.stdout.readline(), timeout=5)
    if line.strip() != b"LOCKED":
        stderr = b""
        if proc.stderr is not None:
            stderr = await proc.stderr.read()
        raise AssertionError(f"child failed to acquire signer lock: {stderr.decode(errors='replace')}")
    return proc


async def _enter_lock(signer: str, entered: asyncio.Event):
    async with signer_action_lock(signer):
        entered.set()


@pytest.mark.asyncio
async def test_same_signer_is_serialized_across_independent_processes():
    signer = "0x" + "11" * 20
    proc = await _start_lock_holder(signer)
    entered = asyncio.Event()
    contender = asyncio.create_task(_enter_lock(signer.upper(), entered))

    # The parent uses a separate process/DB connection and must remain blocked
    # while the child owns the same signer advisory key. Case is normalized.
    await asyncio.sleep(0.15)
    assert not entered.is_set()

    assert await asyncio.wait_for(proc.wait(), timeout=5) == 0
    await asyncio.wait_for(entered.wait(), timeout=2)
    await contender


@pytest.mark.asyncio
async def test_different_signers_remain_concurrent_across_processes():
    signer_a = "0x" + "11" * 20
    signer_b = "0x" + "22" * 20
    assert signer_lock_id(signer_a) != signer_lock_id(signer_b)

    proc = await _start_lock_holder(signer_a, hold_seconds=1.0)
    entered = asyncio.Event()
    contender = asyncio.create_task(_enter_lock(signer_b, entered))

    # A different API wallet receives a different advisory key and must not be
    # serialized behind the first signer.
    await asyncio.wait_for(entered.wait(), timeout=0.5)
    assert proc.returncode is None
    await contender
    assert await asyncio.wait_for(proc.wait(), timeout=5) == 0
