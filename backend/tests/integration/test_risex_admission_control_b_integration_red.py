from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from eth_account import Account
from fastapi import HTTPException
from sqlalchemy import select, text
from starlette.requests import Request

from app.adapters.hyperliquid import HyperliquidAdapter
from app.api import user as user_api
from app.db.session import SessionLocal, engine
from app.models.entities import (
    CredentialStatus,
    RISExSigningCredential,
    RISExTradingAccount,
    SigningCredential,
    TradingAccount,
    User,
)
from app.schemas.user import RISExTradingAccountIn, TradingProviderIn
from app.services import risex_order_preparation
from app.services.execution_destination import set_user_destination, user_destination_state


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INTEGRATION") != "1",
    reason="requires CI PostgreSQL",
)


def _request() -> Request:
    return Request({"type": "http", "client": ("127.0.0.1", 12345), "headers": []})


def _unique_wallet() -> str:
    return "0x" + uuid.uuid4().hex + uuid.uuid4().hex[:8]


def _signer_private_key(user_id: uuid.UUID) -> str:
    return hashlib.sha256(user_id.bytes + b"risex-b-red-signer").hexdigest()


def _set_live_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("ENABLE_LIVE_TRADING", raising=False)
    else:
        monkeypatch.setenv("ENABLE_LIVE_TRADING", value)


async def _insert_user(db, *, network: str = "testnet") -> User:
    user_id = uuid.uuid4()
    wallet = _unique_wallet()
    await db.execute(
        text(
            """
            INSERT INTO users (
                id, auth_wallet, role, state, copy_state, manual_trade_policy,
                execution_network, network_started_at, execution_provider,
                created_at, updated_at
            ) VALUES (
                :user_id, :wallet, 'USER', 'ACTIVE', 'PAUSED', 'COEXIST',
                :network, now(), 'hyperliquid', now(), now()
            )
            """
        ),
        {"user_id": user_id, "wallet": wallet, "network": network},
    )
    await db.commit()
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


async def _activate_hyperliquid(db, user: User, *, network: str) -> uuid.UUID:
    account = TradingAccount(
        user_id=user.id,
        account_address=user.auth_wallet.lower(),
        agent_address="0x" + ("ab" * 20),
        agent_name="risex-b-red",
    )
    db.add(account)
    await db.flush()
    db.add(
        SigningCredential(
            trading_account_id=account.id,
            ciphertext_b64="hl-ciphertext",
            nonce_b64="hl-nonce",
            wrapped_dek_b64="hl-wrapped",
            wrap_nonce_b64="hl-wrapnonce",
            key_provider="test",
            key_reference="hl-test-key",
            key_version=7,
            agent_fingerprint="f" * 64,
            expires_at=datetime.now(UTC) + timedelta(days=30),
            status=CredentialStatus.ACTIVE,
        )
    )
    destination = await set_user_destination(
        db,
        user.id,
        provider="hyperliquid",
        network=network,  # type: ignore[arg-type]
        account_address=user.auth_wallet.lower(),
        credential_version=7,
    )
    await db.commit()
    return destination.epoch_id


async def _add_risex_credential(
    db,
    user: User,
    *,
    status: CredentialStatus = CredentialStatus.ACTIVE,
    expires_at: datetime | None = None,
    generation: int = 4,
) -> tuple[RISExTradingAccount, RISExSigningCredential, str]:
    private_key = _signer_private_key(user.id)
    signer_address = Account.from_key(private_key).address
    account = RISExTradingAccount(
        user_id=user.id,
        account_address=user.auth_wallet.lower(),
    )
    db.add(account)
    await db.flush()
    credential = RISExSigningCredential(
        risex_trading_account_id=account.id,
        signer_address=signer_address,
        ciphertext_b64="risex-ciphertext",
        nonce_b64="risex-nonce",
        wrapped_dek_b64="risex-wrapped",
        wrap_nonce_b64="risex-wrapnonce",
        key_provider="test",
        key_reference="risex-test-key",
        key_version=91,
        generation=generation,
        expires_at=expires_at,
        status=status,
    )
    db.add(credential)
    await db.flush()
    return account, credential, private_key


def _stub_flat_hyperliquid(
    monkeypatch: pytest.MonkeyPatch,
    observed: dict,
) -> None:
    async def user_state(*_args, **_kwargs):
        observed["hl_user_state_calls"] = observed.get("hl_user_state_calls", 0) + 1
        return {"assetPositions": []}

    async def frontend_open_orders(*_args, **_kwargs):
        observed["hl_order_calls"] = observed.get("hl_order_calls", 0) + 1
        return []

    monkeypatch.setattr(HyperliquidAdapter, "user_state", user_state)
    monkeypatch.setattr(HyperliquidAdapter, "frontend_open_orders", frontend_open_orders)


def _stub_valid_risex_verification(
    monkeypatch: pytest.MonkeyPatch,
    observed: dict,
) -> None:
    async def verify(*_args, **kwargs):
        observed.setdefault("verification_calls", []).append(
            (str(kwargs["account_address"]), str(kwargs["signer_address"]))
        )
        return SimpleNamespace(
            account=str(kwargs["account_address"]),
            signer=str(kwargs["signer_address"]),
            session_active=True,
            session_not_expired=True,
            perps_permission=True,
            move_fund_permission=True,
            session_expiration=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        )

    monkeypatch.setattr(user_api, "_verify_risex_signer_binding", verify)


def _stub_crypto(monkeypatch: pytest.MonkeyPatch, observed: dict) -> None:
    def encrypt(plaintext: str, *, user_id: str, account_id: str):
        observed.setdefault("encrypted", []).append(
            {"plaintext": plaintext, "user_id": user_id, "account_id": account_id}
        )
        return SimpleNamespace(
            ciphertext_b64="ciphertext-not-plaintext",
            nonce_b64="nonce",
            wrapped_dek_b64="wrapped",
            wrap_nonce_b64="wrapnonce",
            key_provider="test",
            key_reference="test-key",
            key_version=91,
        )

    monkeypatch.setattr(user_api, "crypto", SimpleNamespace(encrypt=encrypt))


async def _risex_counts(db, user_id: uuid.UUID) -> tuple[int, int]:
    accounts = (
        await db.execute(
            text("SELECT count(*) FROM risex_trading_accounts WHERE user_id = :user_id"),
            {"user_id": user_id},
        )
    ).scalar_one()
    credentials = (
        await db.execute(
            text(
                """
                SELECT count(*)
                FROM risex_signing_credentials c
                JOIN risex_trading_accounts a ON a.id = c.risex_trading_account_id
                WHERE a.user_id = :user_id
                """
            ),
            {"user_id": user_id},
        )
    ).scalar_one()
    return int(accounts), int(credentials)


async def _epoch_count(db, user_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                text("SELECT count(*) FROM execution_epochs WHERE user_id = :user_id"),
                {"user_id": user_id},
            )
        ).scalar_one()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CredentialStatus.ACTIVE, CredentialStatus.EXPIRING])
async def test_put_risex_uses_verified_account_and_logical_generation(
    monkeypatch: pytest.MonkeyPatch,
    status: CredentialStatus,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            await _activate_hyperliquid(db, user, network="testnet")
            risex_account, risex_credential, _private_key = await _add_risex_credential(
                db,
                user,
                status=status,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                generation=4,
            )
            await db.commit()

            await user_api.trading_provider(
                TradingProviderIn(provider="risex"),
                user=user,
                db=db,
            )

            epoch = (
                await db.execute(
                    text(
                        """
                        SELECT e.provider, e.network, e.account_address, e.credential_version
                        FROM users u
                        JOIN execution_epochs e ON e.id = u.active_execution_epoch_id
                        WHERE u.id = :user_id
                        """
                    ),
                    {"user_id": user.id},
                )
            ).mappings().one()

            assert observed.get("verification_calls") == [
                (risex_account.account_address, risex_credential.signer_address)
            ]
            assert epoch["provider"] == "risex"
            assert epoch["network"] == "testnet"
            assert epoch["account_address"].lower() == risex_account.account_address.lower()
            assert epoch["credential_version"] == risex_credential.generation == 4
            assert epoch["credential_version"] != risex_credential.key_version

            # Existing Hyperliquid cleanup semantics are intentionally preserved.
            hl_count = (
                await db.execute(
                    text("SELECT count(*) FROM trading_accounts WHERE user_id = :user_id"),
                    {"user_id": user.id},
                )
            ).scalar_one()
            assert hl_count == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_risex_without_linked_credential_is_409_and_keeps_source_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            source_epoch = await _activate_hyperliquid(db, user, network="testnet")

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert current.provider == "hyperliquid"
            hl_count = (
                await db.execute(
                    text("SELECT count(*) FROM trading_accounts WHERE user_id = :user_id"),
                    {"user_id": user.id},
                )
            ).scalar_one()
            assert hl_count == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        CredentialStatus.EXPIRED,
        CredentialStatus.REVOKED,
        CredentialStatus.DISABLED,
    ],
)
async def test_put_risex_rejects_nonusable_credential_status(
    monkeypatch: pytest.MonkeyPatch,
    status: CredentialStatus,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            source_epoch = await _activate_hyperliquid(db, user, network="testnet")
            await _add_risex_credential(
                db,
                user,
                status=status,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await db.commit()

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert observed.get("verification_calls", []) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_risex_rejects_expired_timestamp_even_if_status_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            source_epoch = await _activate_hyperliquid(db, user, network="testnet")
            await _add_risex_credential(
                db,
                user,
                status=CredentialStatus.ACTIVE,
                expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
            await db.commit()

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert observed.get("verification_calls", []) == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_risex_fails_closed_when_live_binding_cannot_be_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)

    async def unavailable(*_args, **_kwargs):
        raise TimeoutError("synthetic RISEx verification timeout")

    monkeypatch.setattr(user_api, "_verify_risex_signer_binding", unavailable)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            source_epoch = await _activate_hyperliquid(db, user, network="testnet")
            await _add_risex_credential(
                db,
                user,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await db.commit()

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert current.provider == "hyperliquid"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("network", "live_value"),
    [
        ("testnet", "true"),
        ("testnet", None),
        ("testnet", "garbage"),
        ("mainnet", "false"),
    ],
)
async def test_post_risex_account_environment_gate_is_409_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    network: str,
    live_value: str | None,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    _set_live_env(monkeypatch, live_value)
    observed: dict = {}
    _stub_valid_risex_verification(monkeypatch, observed)
    _stub_crypto(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network=network)
            private_key = _signer_private_key(user.id)
            body = RISExTradingAccountIn(
                account_address=user.auth_wallet,
                signer_private_key=private_key,
            )

            with pytest.raises(HTTPException) as exc_info:
                await user_api.link_risex_trading_account(
                    body,
                    _request(),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            assert observed.get("verification_calls", []) == []
            assert observed.get("encrypted", []) == []
            assert await _risex_counts(db, user.id) == (0, 0)
            assert await _epoch_count(db, user.id) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("network", "live_value"),
    [
        ("testnet", "true"),
        ("testnet", None),
        ("testnet", "garbage"),
        ("mainnet", "false"),
    ],
)
async def test_put_risex_environment_gate_is_409_before_verification_or_switch(
    monkeypatch: pytest.MonkeyPatch,
    network: str,
    live_value: str | None,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    _set_live_env(monkeypatch, live_value)
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network=network)
            source_epoch = await _activate_hyperliquid(db, user, network=network)
            await _add_risex_credential(
                db,
                user,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await db.commit()

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            assert observed.get("verification_calls", []) == []
            assert observed.get("hl_user_state_calls", 0) == 0
            assert observed.get("hl_order_calls", 0) == 0
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert current.provider == "hyperliquid"
            assert await _epoch_count(db, user.id) == 1
    finally:
        await engine.dispose()


async def _activate_risex(
    db,
    user: User,
    *,
    generation: int = 4,
) -> tuple[uuid.UUID, RISExTradingAccount, RISExSigningCredential]:
    account, credential, _private_key = await _add_risex_credential(
        db,
        user,
        status=CredentialStatus.ACTIVE,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        generation=generation,
    )
    destination = await set_user_destination(
        db,
        user.id,
        provider="risex",
        network="testnet",
        account_address=account.account_address,
        credential_version=credential.generation,
    )
    await db.commit()
    return destination.epoch_id, account, credential


@pytest.mark.asyncio
async def test_put_risex_detects_concurrent_credential_rotation_after_live_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            source_epoch = await _activate_hyperliquid(db, user, network="testnet")
            account, credential, _private_key = await _add_risex_credential(
                db,
                user,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                generation=4,
            )
            await db.commit()

            original_signer = credential.signer_address
            rotated_signer = Account.from_key(
                hashlib.sha256(user.id.bytes + b"risex-b-red-rotated").hexdigest()
            ).address

            async def verify_then_rotate(*_args, **kwargs):
                assert str(kwargs["account_address"]).lower() == account.account_address.lower()
                assert str(kwargs["signer_address"]).lower() == original_signer.lower()
                async with SessionLocal() as concurrent_db:
                    await concurrent_db.execute(
                        text(
                            """
                            UPDATE risex_signing_credentials
                            SET signer_address = :rotated_signer,
                                generation = generation + 1,
                                updated_at = now()
                            WHERE id = :credential_id
                            """
                        ),
                        {
                            "rotated_signer": rotated_signer,
                            "credential_id": credential.id,
                        },
                    )
                    await concurrent_db.commit()
                observed["rotated"] = True
                return SimpleNamespace(
                    account=account.account_address,
                    signer=original_signer,
                    session_active=True,
                    session_not_expired=True,
                    perps_permission=True,
                    move_fund_permission=True,
                    session_expiration=int(
                        (datetime.now(UTC) + timedelta(hours=1)).timestamp()
                    ),
                )

            monkeypatch.setattr(
                user_api,
                "_verify_risex_signer_binding",
                verify_then_rotate,
            )

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            assert observed.get("rotated") is True
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert current.provider == "hyperliquid"
            assert await _epoch_count(db, user.id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_risex_idempotent_path_reverifies_active_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            epoch_id, account, credential = await _activate_risex(db, user)

            await user_api.trading_provider(
                TradingProviderIn(provider="risex"),
                user=user,
                db=db,
            )

            current = await user_destination_state(db, user.id)
            assert current.epoch_id == epoch_id
            assert current.provider == "risex"
            assert observed.get("verification_calls") == [
                (account.account_address, credential.signer_address)
            ]
            assert await _epoch_count(db, user.id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_risex_idempotent_path_rejects_revoked_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            epoch_id, _account, credential = await _activate_risex(db, user)
            credential.status = CredentialStatus.REVOKED
            await db.commit()

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == epoch_id
            assert current.provider == "risex"
            assert observed.get("verification_calls", []) == []
            assert await _epoch_count(db, user.id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_put_risex_idempotent_path_rejects_closed_environment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    _set_live_env(monkeypatch, "true")
    observed: dict = {}
    _stub_valid_risex_verification(monkeypatch, observed)

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            epoch_id, _account, _credential = await _activate_risex(db, user)

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == epoch_id
            assert current.provider == "risex"
            assert observed.get("verification_calls", []) == []
            assert await _epoch_count(db, user.id) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["account", "signer"])
async def test_put_risex_rejects_low_level_identity_evidence_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    monkeypatch.setattr(
        risex_order_preparation,
        "ADR_0006_MAINNET_GATE_ACCEPTED",
        False,
    )
    monkeypatch.setenv("ENABLE_LIVE_TRADING", "false")
    observed: dict = {}
    _stub_flat_hyperliquid(monkeypatch, observed)

    class _AsyncContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyHTTPTransport",
        lambda **_kwargs: _AsyncContext(),
    )
    monkeypatch.setattr(
        user_api,
        "RISExReadOnlyRPCTransport",
        lambda **_kwargs: _AsyncContext(),
    )

    async def collect_runtime(*_args, **_kwargs):
        return SimpleNamespace(
            domain_verifying_contract="0x" + ("77" * 20),
            block_number=1,
        )

    def evaluate_preflight(*_args, **_kwargs):
        return SimpleNamespace(
            verdict="PASS",
            deployment_identity_verified=True,
        )

    monkeypatch.setattr(
        user_api,
        "collect_runtime_deployment_evidence",
        collect_runtime,
    )
    monkeypatch.setattr(
        user_api,
        "evaluate_pinned_deployment_preflight",
        evaluate_preflight,
    )

    try:
        async with SessionLocal() as db:
            user = await _insert_user(db, network="testnet")
            source_epoch = await _activate_hyperliquid(db, user, network="testnet")
            account, credential, _private_key = await _add_risex_credential(
                db,
                user,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await db.commit()

            evidence_account = (
                "0x" + ("88" * 20)
                if mismatch == "account"
                else account.account_address
            )
            evidence_signer = (
                "0x" + ("99" * 20)
                if mismatch == "signer"
                else credential.signer_address
            )

            async def collect_authorization(*_args, **_kwargs):
                observed["low_level_evidence_called"] = True
                return SimpleNamespace(
                    account=evidence_account,
                    signer=evidence_signer,
                    session_active=True,
                    session_not_expired=True,
                    perps_permission=True,
                    move_fund_permission=True,
                )

            monkeypatch.setattr(
                user_api,
                "collect_authorization_session_evidence",
                collect_authorization,
            )

            with pytest.raises(HTTPException) as exc_info:
                await user_api.trading_provider(
                    TradingProviderIn(provider="risex"),
                    user=user,
                    db=db,
                )

            assert exc_info.value.status_code == 409
            assert observed.get("low_level_evidence_called") is True
            current = await user_destination_state(db, user.id)
            assert current.epoch_id == source_epoch
            assert current.provider == "hyperliquid"
            assert await _epoch_count(db, user.id) == 1
    finally:
        await engine.dispose()
