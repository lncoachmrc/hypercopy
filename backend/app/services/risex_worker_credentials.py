from __future__ import annotations

from dataclasses import dataclass, field
import os
from datetime import UTC, datetime

from eth_account import Account
from eth_account.signers.local import LocalAccount
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import EncryptedCredential, crypto
from app.models.entities import (
    CopyJob,
    CredentialStatus,
    ExecutionEpoch,
    JobState,
    RISExSigningCredential,
    RISExTradingAccount,
    User,
)


class RISExWorkerCredentialResolutionError(RuntimeError):
    def __init__(self, message: str, *, job_state: JobState) -> None:
        super().__init__(message)
        self.job_state = job_state


@dataclass(frozen=True, slots=True)
class RISExResolvedWorkerCredential:
    account_address: str
    signer_address: str
    generation: int
    local_account: LocalAccount = field(repr=False, compare=False)

    def local_account_for_signing(self) -> LocalAccount:
        return self.local_account


def _resolution_error(message: str, *, job_state: JobState) -> RISExWorkerCredentialResolutionError:
    return RISExWorkerCredentialResolutionError(message, job_state=job_state)


async def _generation_mismatch_state(
    db: AsyncSession,
    *,
    user_id,
    epoch_id,
) -> JobState:
    active_epoch_id = (
        await db.execute(
            select(User.active_execution_epoch_id).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    ended_at = (
        await db.execute(
            select(ExecutionEpoch.ended_at).where(ExecutionEpoch.id == epoch_id)
        )
    ).scalar_one_or_none()
    if active_epoch_id != epoch_id or ended_at is not None:
        return JobState.SKIPPED
    return JobState.DEAD


async def resolve_risex_worker_credential(
    db: AsyncSession,
    job: CopyJob,
) -> RISExResolvedWorkerCredential:
    legacy_account = os.environ.get('RISEX_TESTNET_ACCOUNT_ADDRESS')
    legacy_private_key = os.environ.get('RISEX_TESTNET_SIGNER_PRIVATE_KEY')
    if legacy_account and legacy_private_key:
        local_account = Account.from_key(legacy_private_key)
        return RISExResolvedWorkerCredential(
            account_address=legacy_account,
            signer_address=local_account.address,
            generation=1,
            local_account=local_account,
        )

    if job.execution_epoch_id is None:
        raise _resolution_error(
            'RISEx job has no bound execution epoch',
            job_state=JobState.DEAD,
        )

    user = await db.get(User, job.user_id)
    epoch = await db.get(ExecutionEpoch, job.execution_epoch_id)
    if user is None or epoch is None:
        raise _resolution_error(
            'RISEx user or execution epoch is unavailable',
            job_state=JobState.DEAD,
        )
    if (
        epoch.user_id != job.user_id
        or epoch.provider != 'risex'
        or epoch.network != 'testnet'
    ):
        raise _resolution_error(
            'RISEx job execution epoch binding is invalid',
            job_state=JobState.DEAD,
        )
    if user.active_execution_epoch_id != epoch.id or epoch.ended_at is not None:
        raise _resolution_error(
            'RISEx job execution epoch is no longer active',
            job_state=JobState.SKIPPED,
        )

    row = (
        await db.execute(
            select(RISExTradingAccount, RISExSigningCredential)
            .join(
                RISExSigningCredential,
                RISExSigningCredential.risex_trading_account_id
                == RISExTradingAccount.id,
            )
            .where(RISExTradingAccount.user_id == job.user_id)
        )
    ).one_or_none()
    if row is None:
        raise _resolution_error(
            'RISEx per-user credential is unavailable',
            job_state=JobState.DEAD,
        )
    account, credential = row

    if (
        not epoch.account_address
        or account.account_address.lower() != epoch.account_address.lower()
    ):
        raise _resolution_error(
            'RISEx account does not match the active execution epoch',
            job_state=JobState.DEAD,
        )

    if (
        epoch.credential_version is None
        or credential.generation != epoch.credential_version
    ):
        job_state = await _generation_mismatch_state(
            db,
            user_id=job.user_id,
            epoch_id=epoch.id,
        )
        raise _resolution_error(
            'RISEx credential generation does not match the active execution epoch',
            job_state=job_state,
        )

    if credential.status not in {
        CredentialStatus.ACTIVE,
        CredentialStatus.EXPIRING,
    }:
        raise _resolution_error(
            'RISEx credential is not usable',
            job_state=JobState.SKIPPED,
        )
    if credential.expires_at is None or credential.expires_at <= datetime.now(UTC):
        raise _resolution_error(
            'RISEx credential is expired or has no verifiable expiry',
            job_state=JobState.SKIPPED,
        )

    blob = EncryptedCredential(
        ciphertext_b64=credential.ciphertext_b64,
        nonce_b64=credential.nonce_b64,
        wrapped_dek_b64=credential.wrapped_dek_b64,
        wrap_nonce_b64=credential.wrap_nonce_b64,
        key_provider=credential.key_provider,
        key_reference=credential.key_reference,
        key_version=credential.key_version,
    )
    try:
        plaintext = crypto.decrypt(
            blob,
            user_id=str(job.user_id),
            account_id=str(account.id),
        )
    except Exception:
        raise _resolution_error(
            'RISEx credential could not be decrypted',
            job_state=JobState.DEAD,
        ) from None

    try:
        local_account = Account.from_key(plaintext)
    except Exception:
        raise _resolution_error(
            'RISEx credential signer material is invalid',
            job_state=JobState.DEAD,
        ) from None
    finally:
        del plaintext

    if local_account.address.lower() != credential.signer_address.lower():
        raise _resolution_error(
            'RISEx derived signer does not match the stored signer binding',
            job_state=JobState.DEAD,
        )

    return RISExResolvedWorkerCredential(
        account_address=account.account_address,
        signer_address=credential.signer_address,
        generation=credential.generation,
        local_account=local_account,
    )
