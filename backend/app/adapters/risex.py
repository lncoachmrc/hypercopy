from __future__ import annotations

import os
from collections.abc import Callable
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


log = get_logger(__name__)

_WRITE_DISABLED_REASON = (
    'RISEx writes are disabled until the dedicated signer authorization scope '
    'is proven to exclude fund movement'
)


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
        readiness_attestation: RISExRuntimeReadinessAttestation | None = None,
        readiness_clock: Callable[[], float] = _system_clock,
    ) -> None:
        self.network = network
        self.transport = transport
        self.readiness_attestation = readiness_attestation
        self._readiness_clock = readiness_clock

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

    async def place_ioc(
        self,
        *,
        db: Any = None,
        job: Any = None,
        request: RISExPreparedPlaceOrderRequest | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Submit one already-prepared RISEx IOC only through all four fail-closed gates.

        The active execution epoch is the write-network authority. ``self.network`` is
        intentionally not used to authorize a signed mutation, so ambient/global
        network configuration cannot override the job's immutable destination epoch.
        """

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
        try:
            assert_runtime_readiness_attested(
                self.readiness_attestation,
                account_address=request.permit.account_address,
                signer_address=request.permit.signer_address,
                clock=self._readiness_clock,
            )
        except SignedTestnetBlocked as exc:
            self._reject_place_ioc(cause=3, detail=str(exc), job=job)

        if not isinstance(self.transport, RISExSignedTestnetHTTPTransport):
            self._reject_place_ioc(
                cause=4,
                detail='attested signed testnet transport is required',
                job=job,
            )

        try:
            result = await self.transport.post_place_order(request)
        except SignedTestnetBlocked as exc:
            self._reject_place_ioc(cause=4, detail=str(exc), job=job)
        except Exception as exc:
            log.error(
                'RISEx place_ioc attempt',
                extra={
                    'outcome': 'transport_error',
                    'error_type': type(exc).__name__,
                    **self._attempt_context(job),
                },
            )
            raise

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
