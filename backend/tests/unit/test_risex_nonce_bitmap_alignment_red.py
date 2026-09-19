from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.risex_types import ProviderDataMalformed
from app.security.risex_deployment_probe import (
    ContractDeploymentEvidence,
    RISExDeploymentEvidence,
)
from app.security.risex_nonce_state import RISExOrderNonceSelection
from app.security.risex_order_codec import RISExPlaceOrder
from app.security.risex_place_order_permit import prepare_place_order_permit
from app.security.risex_place_order_request import prepare_place_order_request
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_testnet_signer import load_testnet_signer_credential


ACCOUNT = '0x' + ('11' * 20)
AUTH = '0x' + ('33' * 20)
ROUTER = '0x' + ('44' * 20)
AUTH_IMPL = '0x' + ('55' * 20)
ROUTER_IMPL = '0x' + ('66' * 20)
CHAIN_ID = 11155931
BLOCK_NUMBER = 0x1234
BLOCK_TIMESTAMP = 1_900_000_000
SESSION_EXPIRATION = BLOCK_TIMESTAMP + 600
DEADLINE = BLOCK_TIMESTAMP + 30
EXPECTED_FINGERPRINT = 'ab' * 32


class FakeNonceAPI:
    public_read_only = True

    def __init__(
        self,
        *,
        anchor: int = 1,
        current_bitmap_index: int = 2,
        bitmap: str = '0x3',
    ) -> None:
        self.anchor = anchor
        self.current_bitmap_index = current_bitmap_index
        self.bitmap = bitmap

    async def get_json(self, path: str, *, params=None):
        assert params is None
        assert path == f'/v1/nonce-state/{ACCOUNT}'
        return {
            'data': {
                'nonce_anchor': str(self.anchor),
                'current_bitmap_index': self.current_bitmap_index,
                'bitmap': self.bitmap,
            }
        }


def _credential():
    key = bytes([7]) * 32
    return load_testnet_signer_credential(
        {
            'RISEX_TESTNET_ACCOUNT_ADDRESS': ACCOUNT,
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': '0x' + key.hex(),
        }
    )


def _request():
    credential = _credential()
    order = RISExPlaceOrder(
        market_id=1,
        size_steps=100,
        price_ticks=50_000,
        side=0,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=7,
        ttl_units=0,
    )
    selection = RISExOrderNonceSelection(
        observed_nonce_anchor=1,
        observed_bitmap_index=2,
        observed_bitmap=3,
        selected_nonce_anchor=1,
        selected_bitmap_index=2,
        rolled_anchor=False,
    )
    permit = prepare_place_order_permit(
        order=order,
        credential=credential,
        nonce_selection=selection,
        domain_name='RISEx',
        domain_version='1',
        chain_id=CHAIN_ID,
        verifying_contract=AUTH,
        router=ROUTER,
        observed_block_timestamp=BLOCK_TIMESTAMP,
        session_expiration=SESSION_EXPIRATION,
        deadline=DEADLINE,
    )
    return prepare_place_order_request(order=order, permit=permit)


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


def _deployment() -> RISExDeploymentEvidence:
    return RISExDeploymentEvidence(
        network='testnet',
        api_chain_id=CHAIN_ID,
        rpc_chain_id=CHAIN_ID,
        block_number=BLOCK_NUMBER,
        domain_name='RISEx',
        domain_version='1',
        domain_verifying_contract=AUTH,
        system_auth_contract=AUTH,
        system_router=ROUTER,
        auth=_contract(AUTH, AUTH_IMPL),
        router=_contract(ROUTER, ROUTER_IMPL),
    )


class FakeRPC:
    public_read_only = True

    def __init__(self, module, *, chain_anchor: int, chain_bitmap: int) -> None:
        self.module = module
        self.chain_anchor = chain_anchor
        self.chain_bitmap = chain_bitmap

    async def call(self, method: str, params: list[object]) -> object:
        if method == 'eth_getCode':
            return (
                '0x6000'
                + self.module.IS_NONCE_USED_SELECTOR[2:]
                + self.module.GET_NONCE_STATE_SELECTOR[2:]
                + self.module.VERIFY_WITNESS_TYPEHASH_SELECTOR[2:]
                + self.module.VERIFY_WITNESS_TYPEHASH[2:]
                + '00'
            )

        if method != 'eth_call':
            raise AssertionError(f'unexpected RPC method: {method}')

        call = params[0]
        assert isinstance(call, dict)
        selector = str(call['data'])[:10].lower()

        if selector == self.module.VERIFY_WITNESS_TYPEHASH_SELECTOR.lower():
            return self.module.VERIFY_WITNESS_TYPEHASH
        if selector == self.module.GET_NONCE_STATE_SELECTOR.lower():
            return '0x' + f'{self.chain_anchor:064x}{self.chain_bitmap:064x}'
        if selector == self.module.IS_NONCE_USED_SELECTOR.lower():
            return '0x' + f'{0:064x}'

        raise AssertionError(f'unexpected selector: {selector}')


async def _collect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chain_anchor: int,
    chain_bitmap: int,
):
    from app.security import risex_replay_protection_architecture as module

    async def collect_deployment(*_args: object, **_kwargs: object):
        return _deployment()

    async def collect_authorization(*_args: object, **_kwargs: object):
        return SimpleNamespace(
            block_timestamp=BLOCK_TIMESTAMP,
            session_expiration=SESSION_EXPIRATION,
        )

    monkeypatch.setattr(
        module,
        'collect_runtime_deployment_evidence',
        collect_deployment,
    )
    monkeypatch.setattr(
        module,
        'evaluate_pinned_deployment_preflight',
        lambda *_args, **_kwargs: SimpleNamespace(
            verdict='PASS',
            deployment_identity_verified=True,
            observed_fingerprint=EXPECTED_FINGERPRINT,
        ),
    )
    monkeypatch.setattr(
        module,
        'collect_authorization_session_evidence',
        collect_authorization,
    )

    return await module.collect_replay_protection_architecture_attestation(
        api=FakeNonceAPI(),
        rpc=FakeRPC(
            module,
            chain_anchor=chain_anchor,
            chain_bitmap=chain_bitmap,
        ),
        request=_request(),
        expected_fingerprint=EXPECTED_FINGERPRINT,
    )


@pytest.mark.asyncio
async def test_nonce_selection_exposes_raw_provider_bitmap() -> None:
    from app.security.risex_nonce_state import collect_order_nonce_selection

    selection = await collect_order_nonce_selection(
        FakeNonceAPI(bitmap='0x3'),
        account=ACCOUNT,
    )

    assert selection.observed_nonce_anchor == 1
    assert selection.observed_bitmap_index == 2
    assert selection.observed_bitmap == 3


@pytest.mark.asyncio
async def test_replay_collector_accepts_equal_api_and_chain_bitmaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attestation = await _collect(
        monkeypatch,
        chain_anchor=1,
        chain_bitmap=3,
    )

    assert attestation.nonce_anchor == 1
    assert attestation.nonce_bitmap_index == 2


@pytest.mark.asyncio
async def test_replay_collector_rejects_real_bitmap_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match='nonce state disagree'):
        await _collect(
            monkeypatch,
            chain_anchor=1,
            chain_bitmap=7,
        )


@pytest.mark.asyncio
async def test_replay_collector_rejects_anchor_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match='nonce state disagree'):
        await _collect(
            monkeypatch,
            chain_anchor=2,
            chain_bitmap=3,
        )
