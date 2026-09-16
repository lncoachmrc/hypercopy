from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import time as _system_clock
from typing import Any, ClassVar, Literal, NoReturn

from app.adapters.risex_signed_testnet_http import RISExSignedTestnetHTTPTransport
from app.adapters.risex_types import ProviderWriteDisabled, RISExTransport
from app.core.config import Network
from app.core.logging import get_logger
from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_signed_testnet_runner import (
    RISExRuntimeReadinessAttestation,
    assert_runtime_readiness_attested,
)
from app.services.execution_destination import job_matches_active_destination
from app.services.risex_execution_window import RISExExecutionState


log = get_logger(__name__)

_WRITE_DISABLED_REASON = (
    'RISEx writes are disabled until the dedicated signer authorization scope '
    'is proven to exclude fund movement'
)
Gate3Mode = Literal['short_lived_attestation', 'continuous_window']


@dataclass(frozen=True, slots=True)
class _ContinuousOperationSnapshot:
    invalidation_epoch: int
    context_fingerprint: str
    account_address: str
    signer_address: str
    permit_account_address: str
    permit_signer_address: str


class _UnlockedSubmissionBoundary:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        return None


class RISExAdapter:
    provider: ClassVar[Literal['risex']] = 'risex'
    authorization_mode: ClassVar[Literal['registered_signer_permit']] = (
        'registered_signer_permit'
    )
    writes_enabled: ClassVar[Literal[False]] = False

    def __init__(
        self,
        *,
        network: Network,
        transport: RISExTransport | RISExSignedTestnetHTTPTransport | None = None,
        gate3_mode: Gate3Mode | None = None,
        readiness_attestation: RISExRuntimeReadinessAttestation | None = None,
        readiness_clock: Callable[[], float] = _system_clock,
        continuous_authorization: Any = None,
    ) -> None:
        if gate3_mode not in {None, 'short_lived_attestation', 'continuous_window'}:
            raise ValueError('RISEx gate 3 authorization mode is invalid')
        if gate3_mode == 'short_lived_attestation' and continuous_authorization is not None:
            raise ValueError('RISEx short-lived gate 3 mode rejects continuous authorization material')
        if gate3_mode == 'continuous_window' and readiness_attestation is not None:
            raise ValueError('RISEx continuous gate 3 mode rejects readiness attestation material')
        self.network = network
        self.transport = transport
        self._gate3_mode = gate3_mode
        self.readiness_attestation = readiness_attestation
        self._readiness_clock = readiness_clock
        self._continuous_authorization = continuous_authorization

    @property
    def gate3_mode(self) -> Gate3Mode | None:
        return self._gate3_mode

    @staticmethod
    def _write_disabled() -> NoReturn:
        raise ProviderWriteDisabled(_WRITE_DISABLED_REASON)

    @staticmethod
    def _attempt_context(job: object | None) -> dict[str, object]:
        return {
            'execution_epoch_id': str(getattr(job, 'execution_epoch_id', '<none>')),
            'network': str(getattr(job, 'execution_network', '<unknown>')),
        }

    def _reject_place_ioc(
        self,
        *,
        cause: Literal[1, 2, 3, 4],
        detail: str,
        job: object | None,
    ) -> NoReturn:
        log.warning(
            'RISEx place_ioc attempt',
            extra={
                'outcome': 'blocked',
                'cause': cause,
                **self._attempt_context(job),
            },
        )
        raise ProviderWriteDisabled(
            f'RISEx writes are disabled (cause {cause}): {detail}'
        )

    def _continuous_snapshot(
        self,
        *,
        request: RISExPreparedPlaceOrderRequest,
        job: object | None,
    ) -> _ContinuousOperationSnapshot:
        authorization = self._continuous_authorization
        window = getattr(authorization, 'window', None)
        account_address = getattr(authorization, 'account_address', None)
        signer_address = getattr(authorization, 'signer_address', None)
        context_fingerprint = getattr(authorization, 'context_fingerprint', None)
        if (
            window is None
            or not isinstance(account_address, str)
            or not isinstance(signer_address, str)
            or not isinstance(context_fingerprint, str)
        ):
            self._reject_place_ioc(
                cause=3,
                detail='continuous gate 3 authorization material is unavailable or incomplete',
                job=job,
            )

        window.expire_if_needed()
        if window.state != RISExExecutionState.ENABLED:
            self._reject_place_ioc(
                cause=3,
                detail=f'continuous operational window is {window.state.value}',
                job=job,
            )
        if getattr(window, 'context_fingerprint', None) != context_fingerprint:
            window.lock('RISEx continuous authorization context mismatch')
            self._reject_place_ioc(
                cause=3,
                detail='continuous authorization context mismatch',
                job=job,
            )
        if request.permit.account_address.lower() != account_address.lower():
            window.lock('RISEx continuous permit account identity mismatch')
            self._reject_place_ioc(
                cause=3,
                detail='continuous permit account identity mismatch',
                job=job,
            )
        if request.permit.signer_address.lower() != signer_address.lower():
            window.lock('RISEx continuous permit signer identity mismatch')
            self._reject_place_ioc(
                cause=3,
                detail='continuous permit signer identity mismatch',
                job=job,
            )
        return _ContinuousOperationSnapshot(
            invalidation_epoch=int(getattr(window, 'authorization_invalidation_epoch', -1)),
            context_fingerprint=context_fingerprint,
            account_address=account_address,
            signer_address=signer_address,
            permit_account_address=request.permit.account_address,
            permit_signer_address=request.permit.signer_address,
        )

    async def _continuous_final_fence(
        self,
        *,
        snapshot: _ContinuousOperationSnapshot,
        request: RISExPreparedPlaceOrderRequest,
        job: object | None,
    ) -> None:
        authorization = self._continuous_authorization
        window = getattr(authorization, 'window', None)
        if window is None:
            self._reject_place_ioc(
                cause=3,
                detail='continuous operational window is unavailable',
                job=job,
            )

        window.expire_if_needed()
        if window.state != RISExExecutionState.ENABLED:
            self._reject_place_ioc(
                cause=3,
                detail=f'continuous operational window is {window.state.value}',
                job=job,
            )

        current_epoch = int(getattr(window, 'authorization_invalidation_epoch', -1))
        current_context = getattr(window, 'context_fingerprint', None)
        if current_epoch != snapshot.invalidation_epoch:
            window.lock('RISEx continuous authorization epoch changed ambiguously before submission')
            self._reject_place_ioc(
                cause=3,
                detail='continuous authorization invalidation epoch changed before submission',
                job=job,
            )
        if current_context != snapshot.context_fingerprint:
            window.lock('RISEx continuous authorization context changed before submission')
            self._reject_place_ioc(
                cause=3,
                detail='continuous authorization context changed before submission',
                job=job,
            )
        if (
            request.permit.account_address.lower() != snapshot.account_address.lower()
            or request.permit.account_address.lower() != snapshot.permit_account_address.lower()
        ):
            window.lock('RISEx continuous permit account changed before submission')
            self._reject_place_ioc(
                cause=3,
                detail='continuous permit account identity changed before submission',
                job=job,
            )
        if (
            request.permit.signer_address.lower() != snapshot.signer_address.lower()
            or request.permit.signer_address.lower() != snapshot.permit_signer_address.lower()
        ):
            window.lock('RISEx continuous permit signer changed before submission')
            self._reject_place_ioc(
                cause=3,
                detail='continuous permit signer identity changed before submission',
                job=job,
            )

        final_authorizer = getattr(authorization, 'final_authorizer', None)
        if final_authorizer is not None:
            try:
                result = final_authorizer()
                if isinstance(result, Awaitable):
                    await result
            except ProviderWriteDisabled:
                raise
            except Exception as exc:
                if window.state == RISExExecutionState.ENABLED:
                    window.lock('RISEx final continuous authorization failed')
                self._reject_place_ioc(
                    cause=3,
                    detail=f'final continuous authorization failed ({type(exc).__name__})',
                    job=job,
                )

    async def place_ioc(
        self,
        *,
        db: Any = None,
        job: Any = None,
        request: RISExPreparedPlaceOrderRequest | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Submit one already-prepared RISEx IOC only through all fail-closed gates."""

        if os.environ.get('RISEX_SIGNED_WRITES_ENABLED') != 'true':
            self._reject_place_ioc(
                cause=1,
                detail='RISEX_SIGNED_WRITES_ENABLED must be explicitly set to true',
                job=job,
            )

        if db is None or job is None:
            self._reject_place_ioc(
                cause=2,
                detail='active RISEx execution destination epoch is unavailable',
                job=job,
            )

        try:
            destination_matches = await job_matches_active_destination(db, job)
        except Exception as exc:
            self._reject_place_ioc(
                cause=2,
                detail=(
                    'active RISEx execution destination epoch could not be verified '
                    f'({type(exc).__name__})'
                ),
                job=job,
            )
        if not destination_matches:
            self._reject_place_ioc(
                cause=2,
                detail='order does not match the active execution destination epoch',
                job=job,
            )

        execution_provider = getattr(job, 'execution_provider', None)
        execution_network = getattr(job, 'execution_network', None)
        execution_epoch_id = getattr(job, 'execution_epoch_id', None)
        if (
            execution_provider != 'risex'
            or execution_network != 'testnet'
            or execution_epoch_id is None
        ):
            self._reject_place_ioc(
                cause=2,
                detail='active execution epoch must resolve to RISEx testnet',
                job=job,
            )

        if not isinstance(request, RISExPreparedPlaceOrderRequest):
            self._reject_place_ioc(
                cause=3,
                detail='a typed prepared RISEx place-order request is required',
                job=job,
            )
        if self._gate3_mode is None:
            self._reject_place_ioc(
                cause=3,
                detail='an explicit gate 3 authorization mode is required',
                job=job,
            )

        if not isinstance(self.transport, RISExSignedTestnetHTTPTransport):
            self._reject_place_ioc(
                cause=4,
                detail='attested signed testnet transport is required',
                job=job,
            )

        if self._gate3_mode == 'short_lived_attestation':
            try:
                assert_runtime_readiness_attested(
                    self.readiness_attestation,
                    account_address=request.permit.account_address,
                    signer_address=request.permit.signer_address,
                    clock=self._readiness_clock,
                )
            except SignedTestnetBlocked as exc:
                self._reject_place_ioc(cause=3, detail=str(exc), job=job)
            try:
                result = await self.transport.post_place_order(request)
            except SignedTestnetBlocked as exc:
                self._reject_place_ioc(cause=4, detail=str(exc), job=job)
        else:
            snapshot = self._continuous_snapshot(request=request, job=job)
            try:
                payload = await self.transport.prepare_place_order_post(request)
            except SignedTestnetBlocked as exc:
                self._reject_place_ioc(cause=4, detail=str(exc), job=job)

            boundary = getattr(self._continuous_authorization, 'submission_lock', None)
            if boundary is None:
                boundary = _UnlockedSubmissionBoundary()
            async with boundary:
                await self._continuous_final_fence(
                    snapshot=snapshot,
                    request=request,
                    job=job,
                )
                try:
                    result = await self.transport.post_prepared_place_order(payload)
                except SignedTestnetBlocked as exc:
                    self._reject_place_ioc(cause=4, detail=str(exc), job=job)

        log.info(
            'RISEx place_ioc attempt',
            extra={
                'outcome': 'submitted',
                **self._attempt_context(job),
            },
        )
        return result

    async def update_leverage(self, **_kwargs: Any) -> NoReturn:
        self._write_disabled()

    async def cancel_order(self, **_kwargs: Any) -> NoReturn:
        self._write_disabled()

    async def register_signer(self, **_kwargs: Any) -> NoReturn:
        self._write_disabled()

    async def revoke_signer(self, **_kwargs: Any) -> NoReturn:
        self._write_disabled()

    async def approve_builder_fee(self, **_kwargs: Any) -> NoReturn:
        self._write_disabled()
