from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.requests import Request

import app.api.user as user_api
from app.db.session import SessionLocal, engine
from app.models.entities import CopyJob, Execution, ExecutionEpoch, ExecutionState, JobState, User
from app.schemas.user import RISExTradingAccountIn
from app.security.risex_deployment_preflight import canonical_deployment_fingerprint
from app.security.risex_deployment_runtime import collect_runtime_deployment_evidence
from app.security.risex_order_codec import RISExPlaceOrder, build_place_order_action_hash
from app.security.risex_place_order_permit import RISExPreparedPlaceOrderPermit
from app.security.risex_place_order_request import prepare_place_order_request
from app.services import (
    risex_copy_execution,
    risex_order_preparation,
    risex_worker_credentials,
)
from app.services.destination_switch import destination_switch_blockers
from tests.unit.test_risex_signed_testnet_runner import FakeAPI, FakeRPC


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


def _request() -> Request:
    return Request({"type": "http", "client": ("127.0.0.1", 12345), "headers": []})


def _wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _private_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


async def _patch_real_verifiers_with_fake_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = await collect_runtime_deployment_evidence(
        FakeAPI(),
        FakeRPC(permissions={1: True, 2: True, 3: True, 4: True}),
        network="testnet",
    )
    fingerprint = canonical_deployment_fingerprint(expected)
    assert fingerprint is not None
    monkeypatch.setattr(
        user_api,
        "PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT",
        fingerprint,
    )
    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyHTTPTransport",
        lambda **_kwargs: _AsyncContext(FakeAPI()),
    )
    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyRPCTransport",
        lambda **_kwargs: _AsyncContext(
            FakeRPC(permissions={1: True, 2: True, 3: True, 4: True})
        ),
    )


def _patch_encrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    def encrypt(_plaintext: str, *, user_id: str, account_id: str):
        assert user_id
        assert account_id
        return SimpleNamespace(
            ciphertext_b64="ciphertext",
            nonce_b64="nonce",
            wrapped_dek_b64="wrapped",
            wrap_nonce_b64="wrapnonce",
            key_provider="test",
            key_reference="test-key",
            key_version=91,
        )

    monkeypatch.setattr(user_api, "crypto", SimpleNamespace(encrypt=encrypt))


async def _insert_user() -> uuid.UUID:
    user_id = uuid.uuid4()
    wallet = _wallet()
    async with SessionLocal() as db:
        await db.execute(
            text(
                """
                INSERT INTO users (
                    id, auth_wallet, role, state, copy_state, manual_trade_policy,
                    execution_network, network_started_at, execution_provider,
                    created_at, updated_at
                ) VALUES (
                    :user_id, :wallet, 'USER', 'ACTIVE', 'PAUSED', 'COEXIST',
                    'testnet', now(), 'hyperliquid', now(), now()
                )
                """
            ),
            {"user_id": user_id, "wallet": wallet},
        )
        await db.commit()
    return user_id


async def _link_or_rotate(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: uuid.UUID,
    private_key: str,
) -> tuple[uuid.UUID, str, str]:
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    await _patch_real_verifiers_with_fake_transports(monkeypatch)
    _patch_encrypt(monkeypatch)
    async with SessionLocal() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        body = RISExTradingAccountIn.model_validate(
            {
                "account_address": user.auth_wallet,
                "signer_private_key": private_key,
            }
        )
        await user_api.link_risex_trading_account(body, _request(), user, db)
        epoch_id = (
            await db.execute(
                text("SELECT active_execution_epoch_id FROM users WHERE id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar_one()
        return epoch_id, user.auth_wallet, Account.from_key(private_key).address


def _material(
    *,
    execution_id: uuid.UUID,
    account: str,
    signer: str,
    cloid: str,
    client_order_id: int,
):
    market = risex_order_preparation.RISExMarketMetadata(
        market_id=1,
        step_size=Decimal("0.000001"),
        step_price=Decimal("0.1"),
        min_order_size=Decimal("0.0001"),
        max_leverage=Decimal("50"),
        mark_price=Decimal("86192.08"),
    )
    intent = risex_order_preparation.RISExOrderIntent(
        symbol="BTC",
        is_buy=True,
        requested_size=Decimal("0.000100"),
        reduce_only=False,
        slippage_bps=25,
        client_order_id=client_order_id,
    )
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=861_920,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=client_order_id,
        ttl_units=0,
    )
    plan = risex_order_preparation.RISExIOCPlan(
        order=order,
        requested_size=Decimal("0.000100"),
        limit_price=Decimal("86192.0"),
        market=market,
    )
    permit = RISExPreparedPlaceOrderPermit(
        account_address=account,
        signer_address=signer,
        action_hash=build_place_order_action_hash(order),
        nonce_anchor=7,
        nonce_bitmap_index=13,
        deadline=2_000_000_000,
        _signature=bytes([9]) * 65,
    )
    request = prepare_place_order_request(order=order, permit=permit)
    return risex_copy_execution.RISExPreparedCopySubmission(
        execution_id=execution_id,
        cloid=cloid,
        client_order_id=client_order_id,
        intent=intent,
        plan=plan,
        request=request,
    )


async def _seed_job(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    user_id = await _insert_user()
    key = _private_key()
    epoch_id, account, signer = await _link_or_rotate(
        monkeypatch,
        user_id=user_id,
        private_key=key,
    )
    job_id = uuid.uuid4()
    async with SessionLocal() as db:
        db.add(
            CopyJob(
                id=job_id,
                user_id=user_id,
                execution_epoch_id=epoch_id,
                execution_provider="risex",
                execution_network="testnet",
                asset="BTC",
                origin="EVENT",
                state=JobState.PROCESSING,
                owner="part-c-red",
                attempt_count=1,
                correlation_id=uuid.uuid4().hex,
                context={"execution_provider": "risex", "follower_network": "testnet"},
            )
        )
        await db.commit()
    return {
        "user_id": user_id,
        "old_epoch_id": epoch_id,
        "job_id": job_id,
        "account": account,
        "signer": signer,
        "private_key": key,
    }


async def _persist_pre_post(seeded: dict[str, object]) -> dict[str, object]:
    execution_id = uuid.uuid4()
    cloid = "0x" + uuid.uuid4().hex
    client_order_id = 1 + (uuid.uuid4().int % ((1 << 63) - 1))
    async with SessionLocal() as db:
        db.add(
            Execution(
                id=execution_id,
                copy_job_id=seeded["job_id"],
                user_id=seeded["user_id"],
                execution_epoch_id=seeded["old_epoch_id"],
                execution_provider="risex",
                execution_network="testnet",
                attempt_kind="o",
                cloid=cloid,
                client_order_id=client_order_id,
                nonce_anchor=7,
                nonce_bitmap_index=13,
                state=ExecutionState.SUBMITTING,
                asset="BTC",
                is_buy=True,
                requested_size=Decimal("0.000100"),
                reduce_only=False,
                limit_px=Decimal("86192.0"),
                reserved_exposure_usdc=Decimal("0"),
                response={"risex_4b_bis": {"submission_status": "PRE_POST_COMMITTED"}},
            )
        )
        await db.commit()
    seeded.update(
        {
            "execution_id": execution_id,
            "cloid": cloid,
            "client_order_id": client_order_id,
            "submission": _material(
                execution_id=execution_id,
                account=str(seeded["account"]),
                signer=str(seeded["signer"]),
                cloid=cloid,
                client_order_id=client_order_id,
            ),
        }
    )
    return seeded


async def _seed_pre_post(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    return await _persist_pre_post(await _seed_job(monkeypatch))


async def _allow_strategy_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(**_kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(
        risex_copy_execution,
        "current_strategy_intent_for_cloid",
        allowed,
    )


class _NoPostAdapter:
    def __init__(self) -> None:
        self.calls = 0

    async def place_ioc(self, **_kwargs):
        self.calls += 1
        raise AssertionError(
            "RED: superseded credential reached the provider POST path"
        )


async def _assert_definitive_skip(
    *,
    job_id: object,
    execution_id: object,
    adapter: _NoPostAdapter,
) -> None:
    async with SessionLocal() as db:
        job = await db.get(CopyJob, job_id)
        execution = await db.get(Execution, execution_id)
        assert job is not None
        assert execution is not None
        assert job.state == JobState.SKIPPED
        assert adapter.calls == 0
        assert execution.state not in {
            ExecutionState.SUBMITTING,
            ExecutionState.UNKNOWN,
        }
        assert "awaiting provider-truth reconciliation" not in str(job.last_error or "")


@pytest.mark.asyncio
async def test_rotation_completes_first_blocks_old_epoch_post_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_job(monkeypatch)

        # Outcome (a) is specifically rotation after signing but before the durable
        # PRE_POST_COMMITTED row. Once that row exists it is an unresolved execution,
        # and the GREEN rotation path must reject instead of completing the rotation.
        await _link_or_rotate(
            monkeypatch,
            user_id=seeded["user_id"],
            private_key=_private_key(),
        )
        seeded = await _persist_pre_post(seeded)

        await _allow_strategy_intent(monkeypatch)
        adapter = _NoPostAdapter()
        async with SessionLocal() as worker_db:
            job = await worker_db.get(CopyJob, seeded["job_id"])
            result = await risex_copy_execution.process_risex_job(
                worker_db,
                adapter,
                job,
                submission=seeded["submission"],
            )
        assert result == JobState.SKIPPED.value
        await _assert_definitive_skip(
            job_id=seeded["job_id"],
            execution_id=seeded["execution_id"],
            adapter=adapter,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_verification_first_rejects_rotation_with_409_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_pre_post(monkeypatch)
        async with SessionLocal() as worker_db:
            job = await worker_db.get(CopyJob, seeded["job_id"])
            claimed = await risex_copy_execution.claim_risex_first_post(
                worker_db,
                job=job,
                submission=seeded["submission"],
            )
            assert claimed is not None

        with pytest.raises(HTTPException) as exc:
            await _link_or_rotate(
                monkeypatch,
                user_id=seeded["user_id"],
                private_key=_private_key(),
            )
        assert exc.value.status_code == 409
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_destination_epoch_changes_first_blocks_old_epoch_post_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_pre_post(monkeypatch)
        new_epoch = uuid.uuid4()
        async with SessionLocal() as api_db:
            await api_db.execute(
                text("UPDATE execution_epochs SET ended_at = now() WHERE id = :epoch_id"),
                {"epoch_id": seeded["old_epoch_id"]},
            )
            api_db.add(
                ExecutionEpoch(
                    id=new_epoch,
                    user_id=seeded["user_id"],
                    provider="hyperliquid",
                    network="testnet",
                    account_address=seeded["account"],
                    credential_version=1,
                    started_at=datetime.now(UTC),
                )
            )
            await api_db.flush()
            await api_db.execute(
                text(
                    """
                    UPDATE users
                    SET execution_provider = 'hyperliquid',
                        active_execution_epoch_id = :new_epoch
                    WHERE id = :user_id
                    """
                ),
                {"new_epoch": new_epoch, "user_id": seeded["user_id"]},
            )
            await api_db.commit()

        await _allow_strategy_intent(monkeypatch)
        adapter = _NoPostAdapter()
        async with SessionLocal() as worker_db:
            job = await worker_db.get(CopyJob, seeded["job_id"])
            result = await risex_copy_execution.process_risex_job(
                worker_db,
                adapter,
                job,
                submission=seeded["submission"],
            )
        assert result == JobState.SKIPPED.value
        await _assert_definitive_skip(
            job_id=seeded["job_id"],
            execution_id=seeded["execution_id"],
            adapter=adapter,
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_final_verification_first_blocks_destination_change_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_pre_post(monkeypatch)
        async with SessionLocal() as worker_db:
            job = await worker_db.get(CopyJob, seeded["job_id"])
            claimed = await risex_copy_execution.claim_risex_first_post(
                worker_db,
                job=job,
                submission=seeded["submission"],
            )
            assert claimed is not None

        async with SessionLocal() as api_db:
            await api_db.execute(
                text("SELECT id FROM users WHERE id = :user_id FOR UPDATE"),
                {"user_id": seeded["user_id"]},
            )
            blockers = await destination_switch_blockers(
                api_db,
                seeded["user_id"],
                seeded["old_epoch_id"],
            )
            assert "unresolved_executions" in {blocker.code for blocker in blockers}
            await api_db.rollback()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_serialization_locks_are_released_before_fake_post_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_pre_post(monkeypatch)
        await _allow_strategy_intent(monkeypatch)
        observed = {"user_lock": False, "execution_lock": False}

        class _LockObservingAdapter:
            async def place_ioc(self, **_kwargs):
                async with SessionLocal() as observer:
                    await observer.execute(
                        text("SELECT id FROM users WHERE id = :user_id FOR UPDATE NOWAIT"),
                        {"user_id": seeded["user_id"]},
                    )
                    observed["user_lock"] = True
                    await observer.execute(
                        text(
                            "SELECT id FROM executions "
                            "WHERE id = :execution_id FOR UPDATE NOWAIT"
                        ),
                        {"execution_id": seeded["execution_id"]},
                    )
                    observed["execution_lock"] = True
                    await observer.rollback()
                return {
                    "order_id": "fake-terminal-no-fill",
                    "filled_quantity": "0",
                }

        async def persist_provider_truth(_db, _execution):
            return {"provider_truth_persisted": True}

        async with SessionLocal() as worker_db:
            job = await worker_db.get(CopyJob, seeded["job_id"])
            result = await risex_copy_execution.process_risex_job(
                worker_db,
                _LockObservingAdapter(),
                job,
                submission=seeded["submission"],
                persist_provider_truth=persist_provider_truth,
            )

        assert observed == {"user_lock": True, "execution_lock": True}
        assert result == JobState.SKIPPED.value
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_claim_refreshes_cached_user_and_epoch_before_superseded_fence_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        seeded = await _seed_pre_post(monkeypatch)
        await _allow_strategy_intent(monkeypatch)
        adapter = _NoPostAdapter()

        async with SessionLocal() as worker_db:
            job = await worker_db.get(CopyJob, seeded["job_id"])
            assert job is not None

            monkeypatch.setattr(
                risex_worker_credentials.crypto,
                "decrypt",
                lambda *_args, **_kwargs: str(seeded["private_key"]),
            )
            resolved = await risex_worker_credentials.resolve_risex_worker_credential(
                worker_db,
                job,
            )
            assert resolved.account_address.lower() == str(seeded["account"]).lower()

            cached_user = await worker_db.get(User, seeded["user_id"])
            cached_epoch = await worker_db.get(ExecutionEpoch, seeded["old_epoch_id"])
            assert cached_user is not None
            assert cached_epoch is not None
            assert cached_user.active_execution_epoch_id == seeded["old_epoch_id"]
            assert cached_epoch.ended_at is None

            replacement_epoch_id = uuid.uuid4()
            async with SessionLocal() as api_db:
                current_epoch = await api_db.get(
                    ExecutionEpoch,
                    seeded["old_epoch_id"],
                )
                assert current_epoch is not None
                current_epoch.ended_at = datetime.now(UTC)
                api_db.add(
                    ExecutionEpoch(
                        id=replacement_epoch_id,
                        user_id=seeded["user_id"],
                        provider="risex",
                        network="testnet",
                        account_address=current_epoch.account_address,
                        credential_version=current_epoch.credential_version,
                        started_at=datetime.now(UTC),
                    )
                )
                await api_db.flush()
                await api_db.execute(
                    text(
                        "UPDATE users "
                        "SET active_execution_epoch_id = :new_epoch "
                        "WHERE id = :user_id"
                    ),
                    {
                        "new_epoch": replacement_epoch_id,
                        "user_id": seeded["user_id"],
                    },
                )
                await api_db.commit()

            result = await risex_copy_execution.process_risex_job(
                worker_db,
                adapter,
                job,
                submission=seeded["submission"],
            )
            assert result == JobState.SKIPPED.value
            assert adapter.calls == 0

            execution = await worker_db.get(Execution, seeded["execution_id"])
            assert execution is not None
            assert execution.state == ExecutionState.CANCELED
            assert job.state == JobState.SKIPPED
    finally:
        await engine.dispose()
