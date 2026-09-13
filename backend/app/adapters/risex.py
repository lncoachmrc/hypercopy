from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal, NoReturn

from app.adapters.risex_types import ProviderWriteDisabled, RISExTransport
from app.core.config import Network

if TYPE_CHECKING:
    from app.security.risex_signed_testnet_runner import RISExRuntimeReadinessAttestation


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
        transport: RISExTransport | None = None,
        readiness_attestation: RISExRuntimeReadinessAttestation | None = None,
    ) -> None:
        self.network = network
        self.transport = transport
        self.readiness_attestation = readiness_attestation

    @staticmethod
    def _write_disabled() -> NoReturn:
        raise ProviderWriteDisabled(_WRITE_DISABLED_REASON)

    async def place_ioc(self, **_kwargs: Any) -> NoReturn:
        self._write_disabled()

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
