from __future__ import annotations

import importlib
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from eth_account import Account
from sqlalchemy import text

from app.db.session import SessionLocal, engine
from app.models.entities import (
    CopyJob,
    CredentialStatus,
    ExecutionEpoch,
    JobState,
    RISExSigningCredential,
    RISExTradingAccount,
)


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


def _wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _private_key() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def _require_credentials_contract():
    try:
        module = importlib.import_module("app.services.risex_worker_credentials")
    except ModuleNotFoundError:
        pytest.fail(
            "RED: app.services.risex_worker_credentials is required for per-order DB credential resolution",
            pytrace=False,
        )
    resolver = getattr(module, "resolve_risex_worker_credential", None)
    error_type = getattr(module, "RISExWorkerCredentialResolutionError", None)
    assert resolver is not None, "RED: resolve_risex_worker_credential is missing"
    assert error_type is not None, "RED: RISExWorkerCredentialResolutionError is missing"
    return module, resolver, error_type


async def _seed_binding(
    *,
    generation: int = 1,
    epoch_version: int | None = None,
    status: CredentialStatus = CredentialStatus.ACTIVE,
    expires_at: datetime | None = None,
    account_address: str | None = None,
    epoch_account_address: str | None = None,
    private_key: str | None = None,
) -> dict[str, object]:
    user_id = uuid.uuid4()
    wallet = account_address or _wallet()
    key = private_key or _private_key()
    signer = Account.from_key(key)
    account_id = uuid.uuid4()
    epoch_id = uuid.uuid4()
    job_id = uuid.uuid4()
    expiry = expires_at if expires_at is not None else datetime.now(UTC) + timedelta(hours=1)

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
                    'testnet', now(), 'risex', now(), now()
                )
                """
            ),
            {"user_id": user_id, "wallet": wallet},
        )
        db.add(
            RISExTradingAccount(
                id=account_id,
                user_id=user_id,
                account_address=wallet,
            )
        )
        db.add(
            RISExSigningCredential(
                risex_trading_account_id=account_id,
                signer_address=signer.address,
                ciphertext_b64="ciphertext",
                nonce_b64="nonce",
                wrapped_dek_b64="wrapped",
                wrap_nonce_b64="wrapnonce",
                key_provider="test",
                key_reference="test-key",
                key_version=7,
                generation=generation,
                expires_at=expiry,
                status=status,
            )
        )
        db.add(
            ExecutionEpoch(
                id=epoch_id,
                user_id=user_id,
                provider="risex",
                network="testnet",
                account_address=epoch_account_address or wallet,
                credential_version=epoch_version if epoch_version is not None else generation,
                started_at=datetime.now(UTC),
            )
        )
        await db.flush()
        await db.execute(
            text(
                "UPDATE users SET active_execution_epoch_id = :epoch_id WHERE id = :user_id"
            ),
            {"epoch_id": epoch_id, "user_id": user_id},
        )
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
        "wallet": wallet,
        "private_key": key,
        "signer": signer.address,
        "account_id": account_id,
        "epoch_id": epoch_id,
        "job_id": job_id,
        "generation": generation,
    }


def _patch_decrypt(monkeypatch: pytest.MonkeyPatch, module, *, private_key: str, observed: dict) -> None:
    def decrypt(_blob, *, user_id: str, account_id: str) -> str:
        observed["user_id"] = user_id
        observed["account_id"] = account_id
        return private_key

    crypto_obj = getattr(module, "crypto", None)
    assert crypto_obj is not None, "RED: resolver must use app.core.crypto as a patchable verifier dependency"
    monkeypatch.setattr(crypto_obj, "decrypt", decrypt)


@pytest.mark.asyncio
async def test_per_order_resolver_uses_active_epoch_account_generation_and_aad_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        module, resolver, _error_type = _require_credentials_contract()
        seeded = await _seed_binding()
        observed: dict = {}
        _patch_decrypt(
            monkeypatch,
            module,
            private_key=str(seeded["private_key"]),
            observed=observed,
        )
        poison_key = _private_key()
        monkeypatch.setenv("RISEX_TESTNET_ACCOUNT_ADDRESS", _wallet())
        monkeypatch.setenv("RISEX_TESTNET_SIGNER_PRIVATE_KEY", poison_key)

        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            resolved = await resolver(db, job)

        assert resolved.account_address.lower() == str(seeded["wallet"]).lower()
        assert resolved.signer_address.lower() == str(seeded["signer"]).lower()
        assert resolved.generation == seeded["generation"]
        assert resolved.local_account.address.lower() == str(seeded["signer"]).lower()
        assert observed == {
            "user_id": str(seeded["user_id"]),
            "account_id": str(seeded["account_id"]),
        }
        assert Account.from_key(poison_key).address.lower() != resolved.signer_address.lower()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_per_order_resolver_rejects_epoch_account_mismatch_dead_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        module, resolver, error_type = _require_credentials_contract()
        seeded = await _seed_binding(epoch_account_address=_wallet())
        observed: dict = {}
        _patch_decrypt(monkeypatch, module, private_key=str(seeded["private_key"]), observed=observed)
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.DEAD
        assert observed == {}, "RED: mismatch must fail before credential decryption"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generation_mismatch_on_still_active_epoch_is_dead_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        module, resolver, error_type = _require_credentials_contract()
        seeded = await _seed_binding(generation=2, epoch_version=1)
        observed: dict = {}
        _patch_decrypt(monkeypatch, module, private_key=str(seeded["private_key"]), observed=observed)
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.DEAD
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generation_mismatch_after_epoch_superseded_is_skipped_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    try:
        module, resolver, error_type = _require_credentials_contract()
        seeded = await _seed_binding(generation=2, epoch_version=1)
        new_epoch_id = uuid.uuid4()
        async with SessionLocal() as db:
            db.add(
                ExecutionEpoch(
                    id=new_epoch_id,
                    user_id=seeded["user_id"],
                    provider="risex",
                    network="testnet",
                    account_address=str(seeded["wallet"]),
                    credential_version=2,
                    started_at=datetime.now(UTC),
                )
            )
            await db.execute(
                text(
                    "UPDATE execution_epochs SET ended_at = now() WHERE id = :old_epoch"
                ),
                {"old_epoch": seeded["epoch_id"]},
            )
            await db.flush()
            await db.execute(
                text(
                    "UPDATE users SET active_execution_epoch_id = :new_epoch WHERE id = :user_id"
                ),
                {"new_epoch": new_epoch_id, "user_id": seeded["user_id"]},
            )
            await db.commit()

        observed: dict = {}
        _patch_decrypt(monkeypatch, module, private_key=str(seeded["private_key"]), observed=observed)
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.SKIPPED
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("status", "expiry"),
    [
        (CredentialStatus.REVOKED, timedelta(hours=1)),
        (CredentialStatus.EXPIRED, timedelta(hours=1)),
        (CredentialStatus.ACTIVE, timedelta(seconds=-1)),
    ],
)
@pytest.mark.asyncio
async def test_nonusable_or_expired_credential_is_skipped_integration(
    monkeypatch: pytest.MonkeyPatch,
    status: CredentialStatus,
    expiry: timedelta,
) -> None:
    try:
        module, resolver, error_type = _require_credentials_contract()
        seeded = await _seed_binding(
            status=status,
            expires_at=datetime.now(UTC) + expiry,
        )
        observed: dict = {}
        _patch_decrypt(monkeypatch, module, private_key=str(seeded["private_key"]), observed=observed)
        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)
        assert exc.value.job_state == JobState.SKIPPED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_derived_signer_mismatch_is_dead_and_secret_safe_integration(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    try:
        module, resolver, error_type = _require_credentials_contract()
        seeded = await _seed_binding()
        secret = _private_key()
        observed: dict = {}
        _patch_decrypt(monkeypatch, module, private_key=secret, observed=observed)

        async with SessionLocal() as db:
            job = await db.get(CopyJob, seeded["job_id"])
            with pytest.raises(error_type) as exc:
                await resolver(db, job)

        assert exc.value.job_state == JobState.DEAD
        assert secret not in str(exc.value)
        assert secret not in caplog.text
        assert not hasattr(resolver, "_cached_local_account")
    finally:
        await engine.dispose()
