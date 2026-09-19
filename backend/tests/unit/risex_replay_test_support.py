from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from app.security.risex_deployment_probe import (
    ContractDeploymentEvidence,
    RISExDeploymentEvidence,
)
from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest


AUTH_IMPL = '0x' + ('77' * 20)
ROUTER_IMPL = '0x' + ('88' * 20)
TEST_FINGERPRINT = 'ab' * 32


class _API:
    public_read_only = True

    def __init__(self, request: RISExPreparedPlaceOrderRequest) -> None:
        self.request = request

    async def get_json(self, path: str, *, params=None):
        assert params is None
        assert path == f'/v1/nonce-state/{self.request.permit.account_address}'
        return {
            'data': {
                'nonce_anchor': str(self.request.permit.nonce_anchor),
                'current_bitmap_index': self.request.permit.nonce_bitmap_index,
            }
        }


class _RPC:
    public_read_only = True

    def __init__(self, module, request: RISExPreparedPlaceOrderRequest) -> None:
        self.module = module
        self.request = request

    async def call(self, method: str, params: list[object]) -> object:
        if method == 'eth_getCode':
            code = ''.join(
                (
                    self.module.IS_NONCE_USED_SELECTOR[2:],
                    self.module.GET_NONCE_STATE_SELECTOR[2:],
                    self.module.VERIFY_WITNESS_TYPEHASH_SELECTOR[2:],
                    self.module.VERIFY_WITNESS_TYPEHASH[2:],
                )
            )
            return '0x6000' + code + '00'
        if method != 'eth_call':
            raise AssertionError(f'unexpected RPC method {method}')

        call = params[0]
        assert isinstance(call, dict)
        selector = str(call['data'])[:10].lower()
        if selector == self.module.VERIFY_WITNESS_TYPEHASH_SELECTOR.lower():
            return self.module.VERIFY_WITNESS_TYPEHASH
        if selector == self.module.GET_NONCE_STATE_SELECTOR.lower():
            return (
                '0x'
                + f'{self.request.permit.nonce_anchor:064x}'
                + f'{self.request.permit.nonce_bitmap_index:064x}'
            )
        if selector == self.module.IS_NONCE_USED_SELECTOR.lower():
            return '0x' + ('00' * 32)
        raise AssertionError(f'unexpected eth_call selector {selector}')


def _contract(address: str, implementation: str) -> ContractDeploymentEvidence:
    return ContractDeploymentEvidence(
        address=address,
        runtime_code_bytes=1000,
        runtime_code_keccak256='0x' + ('aa' * 32),
        implementation=implementation,
        implementation_code_bytes=2000,
        implementation_code_keccak256='0x' + ('bb' * 32),
        abi_verified=None,
        required_functions_present=None,
    )


def make_test_replay_architecture_attestation(
    *,
    request: RISExPreparedPlaceOrderRequest,
    chain_id: int,
    authorization_address: str,
    router_address: str,
):
    """Issue a genuine collector capability using only deterministic test evidence."""

    from app.security import risex_replay_protection_architecture as module

    deployment = RISExDeploymentEvidence(
        network='testnet',
        api_chain_id=chain_id,
        rpc_chain_id=chain_id,
        block_number=0x1234,
        domain_name='RISEx',
        domain_version='1',
        domain_verifying_contract=authorization_address,
        system_auth_contract=authorization_address,
        system_router=router_address,
        auth=_contract(authorization_address, AUTH_IMPL),
        router=_contract(router_address, ROUTER_IMPL),
    )
    block_timestamp = max(1, request.permit.deadline - 10)
    session_expiration = request.permit.deadline + 600

    async def collect_deployment(*_args: object, **_kwargs: object):
        return deployment

    async def collect_authorization(*_args: object, **_kwargs: object):
        return SimpleNamespace(
            block_timestamp=block_timestamp,
            session_expiration=session_expiration,
        )

    def run():
        with (
            patch.object(
                module,
                'collect_runtime_deployment_evidence',
                collect_deployment,
            ),
            patch.object(
                module,
                'evaluate_pinned_deployment_preflight',
                lambda *_args, **_kwargs: SimpleNamespace(
                    verdict='PASS',
                    deployment_identity_verified=True,
                    observed_fingerprint=TEST_FINGERPRINT,
                ),
            ),
            patch.object(
                module,
                'collect_authorization_session_evidence',
                collect_authorization,
            ),
            patch.object(
                module.Account,
                'recover_message',
                return_value=request.permit.signer_address,
            ),
        ):
            return asyncio.run(
                module.collect_replay_protection_architecture_attestation(
                    api=_API(request),
                    rpc=_RPC(module, request),
                    request=request,
                    expected_fingerprint=TEST_FINGERPRINT,
                )
            )

    # Some callers invoke synchronous gate helpers from async pytest tests. Run
    # this isolated collector in its own thread/event-loop in that case.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run()

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(run).result()
