from __future__ import annotations

from typing import Any, ClassVar, Protocol

import httpx
from eth_utils import keccak

from app.adapters.risex_types import (
    ProviderDataMalformed,
    ProviderReadUnavailable,
    ProviderWriteDisabled,
)
from app.security.risex_deployment_probe import (
    ContractDeploymentEvidence,
    RISExDeploymentEvidence,
)


PINNED_RISEX_TESTNET_DEPLOYMENT_FINGERPRINT = (
    '764412dd3ebb2ecb2b2878e3318bce39e9593cbd32108ebefa90e20161722e2f'
)
EIP1967_IMPLEMENTATION_SLOT = (
    '0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc'
)
_ALLOWED_RPC_METHODS = frozenset(
    {
        'eth_chainId',
        'eth_blockNumber',
        'eth_getCode',
        'eth_getStorageAt',
    }
)


class PublicAPITransport(Protocol):
    @property
    def public_read_only(self) -> bool: ...

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


class PublicRPCTransport(Protocol):
    @property
    def public_read_only(self) -> bool: ...

    async def call(self, method: str, params: list[object]) -> object: ...


class RISExReadOnlyRPCTransport:
    """JSON-RPC transport restricted to deployment-identity read methods."""

    public_read_only: ClassVar[bool] = True

    def __init__(
        self,
        *,
        rpc_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.rpc_url = rpc_url
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout_seconds,
        )
        self._next_id = 1

    async def call(self, method: str, params: list[object]) -> object:
        if method not in _ALLOWED_RPC_METHODS:
            raise ProviderWriteDisabled(
                f'RISEx deployment RPC is read-only; method {method!r} is disabled before network I/O'
            )

        request_id = self._next_id
        self._next_id += 1
        body = {
            'jsonrpc': '2.0',
            'id': request_id,
            'method': method,
            'params': params,
        }
        try:
            response = await self._client.post(self.rpc_url, json=body)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderReadUnavailable(
                f'RISEx read-only RPC failed for {method}: {exc}'
            ) from exc

        if not isinstance(payload, dict):
            raise ProviderDataMalformed(f'RISEx RPC {method} did not return a JSON object')
        if payload.get('error') is not None:
            raise ProviderReadUnavailable(f'RISEx RPC {method} returned an error')
        if 'result' not in payload:
            raise ProviderDataMalformed(f'RISEx RPC {method} response has no result')
        return payload['result']

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> 'RISExReadOnlyRPCTransport':
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.aclose()


def _unwrap(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get('data')
    if data is None:
        return payload
    if not isinstance(data, dict):
        raise ProviderDataMalformed('RISEx API data envelope is not an object')
    return data


def _parse_positive_int(value: object, *, field: str, allow_hex: bool = False) -> int:
    if isinstance(value, bool):
        raise ProviderDataMalformed(f'{field} is not a valid integer')
    try:
        if allow_hex and isinstance(value, str) and value.startswith('0x'):
            result = int(value, 16)
        else:
            result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProviderDataMalformed(f'{field} is not a valid integer') from exc
    if result <= 0:
        raise ProviderDataMalformed(f'{field} must be positive')
    return result


def _address(value: object, *, field: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith('0x'):
        raise ProviderDataMalformed(f'{field} is not a valid EVM address')
    try:
        number = int(value[2:], 16)
    except ValueError as exc:
        raise ProviderDataMalformed(f'{field} is not a valid EVM address') from exc
    if number == 0:
        raise ProviderDataMalformed(f'{field} is the zero address')
    return value


def _decode_hex_bytes(value: object, *, field: str, allow_empty: bool = False) -> bytes:
    if not isinstance(value, str) or not value.startswith('0x'):
        raise ProviderDataMalformed(f'{field} is not hex data')
    raw_hex = value[2:]
    if not allow_empty and not raw_hex:
        raise ProviderDataMalformed(f'{field} is empty')
    if len(raw_hex) % 2:
        raise ProviderDataMalformed(f'{field} has odd-length hex data')
    try:
        return bytes.fromhex(raw_hex)
    except ValueError as exc:
        raise ProviderDataMalformed(f'{field} is not valid hex data') from exc


def _implementation_from_slot(value: object) -> str:
    raw = _decode_hex_bytes(value, field='EIP-1967 implementation slot', allow_empty=False)
    if len(raw) > 32:
        raise ProviderDataMalformed('EIP-1967 implementation slot is too large')
    padded = raw.rjust(32, b'\x00')
    implementation = '0x' + padded[-20:].hex()
    return _address(implementation, field='EIP-1967 implementation')


def _code_hash(code: bytes) -> str:
    return '0x' + keccak(code).hex()


async def _contract_evidence(
    rpc: PublicRPCTransport,
    *,
    address: str,
    block_hex: str,
) -> ContractDeploymentEvidence:
    proxy_raw = await rpc.call('eth_getCode', [address, block_hex])
    proxy_code = _decode_hex_bytes(proxy_raw, field='proxy runtime code', allow_empty=False)

    slot_raw = await rpc.call(
        'eth_getStorageAt',
        [address, EIP1967_IMPLEMENTATION_SLOT, block_hex],
    )
    implementation = _implementation_from_slot(slot_raw)
    implementation_raw = await rpc.call('eth_getCode', [implementation, block_hex])
    implementation_code = _decode_hex_bytes(
        implementation_raw,
        field='implementation runtime code',
        allow_empty=False,
    )

    return ContractDeploymentEvidence(
        address=address,
        runtime_code_bytes=len(proxy_code),
        runtime_code_keccak256=_code_hash(proxy_code),
        implementation=implementation,
        implementation_code_bytes=len(implementation_code),
        implementation_code_keccak256=_code_hash(implementation_code),
        abi_verified=None,
        required_functions_present=None,
    )


async def collect_runtime_deployment_evidence(
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    *,
    network: str,
) -> RISExDeploymentEvidence:
    """Collect same-run API/RPC deployment identity evidence with no mutation path."""

    if getattr(api, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx deployment collector requires a public read-only API transport'
        )
    if getattr(rpc, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx deployment collector requires a read-only RPC transport'
        )

    domain = _unwrap(await api.get_json('/v1/auth/eip712-domain'))
    system = _unwrap(await api.get_json('/v1/system/config'))

    domain_name = domain.get('name')
    domain_version = domain.get('version')
    if not isinstance(domain_name, str) or not domain_name:
        raise ProviderDataMalformed('RISEx EIP-712 domain name is missing')
    if not isinstance(domain_version, str) or not domain_version:
        raise ProviderDataMalformed('RISEx EIP-712 domain version is missing')

    api_chain_id = _parse_positive_int(
        domain.get('chain_id', domain.get('chainId')),
        field='RISEx API chainId',
    )
    verifying_contract = _address(
        domain.get('verifying_contract', domain.get('verifyingContract')),
        field='RISEx verifyingContract',
    )

    addresses = system.get('addresses')
    if not isinstance(addresses, dict):
        raise ProviderDataMalformed('RISEx system config addresses are missing')
    system_auth = _address(addresses.get('auth'), field='RISEx system auth')
    system_router = _address(addresses.get('router'), field='RISEx system router')

    rpc_chain_raw = await rpc.call('eth_chainId', [])
    rpc_chain_id = _parse_positive_int(
        rpc_chain_raw,
        field='RISEx RPC chainId',
        allow_hex=True,
    )
    block_raw = await rpc.call('eth_blockNumber', [])
    block_number = _parse_positive_int(
        block_raw,
        field='RISEx RPC block number',
        allow_hex=True,
    )
    if not isinstance(block_raw, str) or not block_raw.startswith('0x'):
        raise ProviderDataMalformed('RISEx RPC block number must be canonical hex')
    block_hex = block_raw

    auth = await _contract_evidence(
        rpc,
        address=verifying_contract,
        block_hex=block_hex,
    )
    router = await _contract_evidence(
        rpc,
        address=system_router,
        block_hex=block_hex,
    )

    return RISExDeploymentEvidence(
        network=network,
        api_chain_id=api_chain_id,
        rpc_chain_id=rpc_chain_id,
        block_number=block_number,
        domain_name=domain_name,
        domain_version=domain_version,
        domain_verifying_contract=verifying_contract,
        system_auth_contract=system_auth,
        system_router=system_router,
        auth=auth,
        router=router,
    )
