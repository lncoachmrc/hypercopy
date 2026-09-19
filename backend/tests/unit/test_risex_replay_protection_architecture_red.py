from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

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


def _module():
    try:
        from app.security import risex_replay_protection_architecture as module
    except ImportError as exc:
        pytest.fail(
            'RED: replay-protection architecture collector is not implemented yet: '
            f'{exc}',
            pytrace=False,
        )
    return module


def _credential():
    signer_key = bytes([7]) * 32
    return load_testnet_signer_credential(
        {
            'RISEX_TESTNET_ACCOUNT_ADDRESS': ACCOUNT,
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': '0x' + signer_key.hex(),
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
    nonce = RISExOrderNonceSelection(
        observed_nonce_anchor=42,
        observed_bitmap_index=17,
        observed_bitmap=0x1FFFF,
        selected_nonce_anchor=42,
        selected_bitmap_index=17,
        rolled_anchor=False,
    )
    permit = prepare_place_order_permit(
        order=order,
        credential=credential,
        nonce_selection=nonce,
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


class FakeAPI:
    public_read_only = True

    async def get_json(self, path: str, *, params=None):
        assert params is None
        assert path == f'/v1/nonce-state/{ACCOUNT}'
        return {
            'data': {
                'nonce_anchor': '42',
                'current_bitmap_index': 17,
                'bitmap': '0x1ffff',
            }
        }


class FakeRPC:
    public_read_only = True

    def __init__(
        self,
        module,
        *,
        nonce_used: bool = False,
        include_get_nonce_state: bool = True,
        typehash_override: str | None = None,
    ) -> None:
        self.module = module
        self.nonce_used = nonce_used
        self.include_get_nonce_state = include_get_nonce_state
        self.typehash_override = typehash_override

    async def call(self, method: str, params: list[object]) -> object:
        if method == 'eth_getCode':
            assert params[0] == AUTH_IMPL
            pieces = [
                self.module.IS_NONCE_USED_SELECTOR[2:],
                self.module.VERIFY_WITNESS_TYPEHASH_SELECTOR[2:],
                self.module.VERIFY_WITNESS_TYPEHASH[2:],
            ]
            if self.include_get_nonce_state:
                pieces.append(self.module.GET_NONCE_STATE_SELECTOR[2:])
            return '0x6000' + ''.join(pieces) + '00'

        if method != 'eth_call':
            raise AssertionError(f'unexpected RPC method: {method}')

        call = params[0]
        assert isinstance(call, dict)
        data = str(call['data'])
        selector = data[:10].lower()

        if selector == self.module.VERIFY_WITNESS_TYPEHASH_SELECTOR.lower():
            value = self.typehash_override or self.module.VERIFY_WITNESS_TYPEHASH
            return value
        if selector == self.module.GET_NONCE_STATE_SELECTOR.lower():
            return '0x' + f'{42:064x}{0x1FFFF:064x}'
        if selector == self.module.IS_NONCE_USED_SELECTOR.lower():
            return '0x' + f'{int(self.nonce_used):064x}'

        raise AssertionError(f'unexpected eth_call selector: {selector}')


async def _collect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    nonce_used: bool = False,
    include_get_nonce_state: bool = True,
    typehash_override: str | None = None,
    deployment_verdict: str = 'PASS',
):
    module = _module()
    deployment = _deployment()

    async def collect_deployment(*_args: object, **_kwargs: object):
        return deployment

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
            verdict=deployment_verdict,
            deployment_identity_verified=deployment_verdict == 'PASS',
            observed_fingerprint=EXPECTED_FINGERPRINT if deployment_verdict == 'PASS' else None,
        ),
    )
    monkeypatch.setattr(
        module,
        'collect_authorization_session_evidence',
        collect_authorization,
    )

    request = _request()
    attestation = await module.collect_replay_protection_architecture_attestation(
        api=FakeAPI(),
        rpc=FakeRPC(
            module,
            nonce_used=nonce_used,
            include_get_nonce_state=include_get_nonce_state,
            typehash_override=typehash_override,
        ),
        request=request,
        expected_fingerprint=EXPECTED_FINGERPRINT,
    )
    return module, request, attestation


def test_replay_architecture_constants_match_live_pinned_interface_probe() -> None:
    module = _module()

    assert module.IS_NONCE_USED_SELECTOR == '0xdcd621a2'
    assert module.GET_NONCE_STATE_SELECTOR == '0x8c1009b5'
    assert module.VERIFY_WITNESS_TYPEHASH_SELECTOR == '0x8110edc1'
    assert (
        module.VERIFY_WITNESS_TYPEHASH
        == '0x055e6bcbf2ba5ff1c2ba5dc95b6648a5de6aaab3185251a34e3b88c11e116821'
    )


def test_collector_boundary_is_explicitly_architectural_not_behavioral() -> None:
    module = _module()

    doc = module.collect_replay_protection_architecture_attestation.__doc__ or ''
    normalized = ' '.join(doc.lower().split())

    assert 'architectural' in normalized
    assert 'does not prove' in normalized
    assert 'second identical submission' in normalized


@pytest.mark.asyncio
async def test_collector_emits_sealed_request_bound_architecture_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, request, attestation = await _collect(monkeypatch)

    assert attestation.network == 'testnet'
    assert attestation.deployment_fingerprint == EXPECTED_FINGERPRINT
    assert attestation.account_address == ACCOUNT
    assert attestation.signer_address == _credential().signer_address
    assert attestation.authorization_address == AUTH
    assert attestation.authorization_implementation == AUTH_IMPL
    assert attestation.router_address == ROUTER
    assert attestation.nonce_anchor == 42
    assert attestation.nonce_bitmap_index == 17
    assert attestation.nonce_currently_unused is True
    assert attestation.deadline == DEADLINE
    assert attestation.action_hash == request.permit.action_hash
    assert attestation.verify_witness_typehash == module.VERIFY_WITNESS_TYPEHASH
    assert attestation.behavioral_replay_rejection_proven is False

    module.assert_replay_protection_architecture_attested(
        attestation,
        request=request,
        account_address=ACCOUNT,
        signer_address=_credential().signer_address,
        chain_id=CHAIN_ID,
        authorization_address=AUTH,
        router_address=ROUTER,
    )


@pytest.mark.asyncio
async def test_replay_architecture_attestation_cannot_be_constructed_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _module_ref, _request_ref, attestation = await _collect(monkeypatch)
    cls = type(attestation)

    with pytest.raises(TypeError):
        cls(
            network='testnet',
            deployment_fingerprint=EXPECTED_FINGERPRINT,
            account_address=ACCOUNT,
        )


@pytest.mark.asyncio
async def test_collector_fails_closed_on_pinned_deployment_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    with pytest.raises(SignedTestnetBlocked, match='deployment'):
        await _collect(monkeypatch, deployment_verdict='FAIL')

    assert module is not None


@pytest.mark.asyncio
async def test_collector_fails_if_nonce_runtime_selectors_are_not_in_bytecode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match='getNonceState|selector'):
        await _collect(monkeypatch, include_get_nonce_state=False)


@pytest.mark.asyncio
async def test_collector_fails_if_verify_witness_typehash_is_not_runtime_coherent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match='typehash'):
        await _collect(
            monkeypatch,
            typehash_override='0x' + ('99' * 32),
        )


@pytest.mark.asyncio
async def test_collector_fails_if_selected_nonce_is_already_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SignedTestnetBlocked, match='nonce.*used|already used'):
        await _collect(monkeypatch, nonce_used=True)


def test_pre_order_gate_no_longer_accepts_a_replay_verified_boolean() -> None:
    from app.security.risex_pre_order_gate import authorize_pre_order_probe

    params = inspect.signature(authorize_pre_order_probe).parameters

    assert 'replay_protection_verified' not in params
    assert 'replay_protection_architecture_attestation' in params
