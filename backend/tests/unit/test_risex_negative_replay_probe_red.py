from __future__ import annotations

import copy
import importlib.util
import inspect
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.security.risex_signed_testnet_policy import SignedTestnetBlocked


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('22' * 20)
ACTION_HASH = '0x' + ('44' * 32)
PRIVATE_KEY = '0x' + ('ab' * 32)
SIGNATURE = 'sensitive-signature'
CLIENT_ORDER_ID = 424242
DEADLINE = 1_900_000_060


def _module() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / 'scripts' / 'risex_negative_replay_probe.py'
    if not path.exists():
        pytest.fail(
            'RED: dedicated RISEx negative replay probe is not implemented yet',
            pytrace=False,
        )
    spec = importlib.util.spec_from_file_location('risex_negative_replay_probe', path)
    if spec is None or spec.loader is None:
        pytest.fail('RED: unable to load RISEx negative replay probe', pytrace=False)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _request() -> SimpleNamespace:
    return SimpleNamespace(
        order=SimpleNamespace(
            market_id=1,
            size_steps=100,
            price_ticks=81_594_900,
            side=0,
            post_only=False,
            reduce_only=False,
            stp_mode=0,
            order_type=1,
            time_in_force=3,
            client_order_id=CLIENT_ORDER_ID,
            ttl_units=0,
        ),
        permit=SimpleNamespace(
            account_address=ACCOUNT,
            signer_address=SIGNER,
            action_hash=ACTION_HASH,
            nonce_anchor=1,
            nonce_bitmap_index=2,
            deadline=DEADLINE,
        ),
    )


def _payload() -> dict[str, object]:
    return {
        'market_id': 1,
        'size_steps': 100,
        'price_ticks': 81_594_900,
        'side': 0,
        'post_only': False,
        'reduce_only': False,
        'stp_mode': 0,
        'order_type': 1,
        'time_in_force': 3,
        'builder_id': 0,
        'client_order_id': str(CLIENT_ORDER_ID),
        'ttl_units': 0,
        'permit': {
            'account': ACCOUNT,
            'signer': SIGNER,
            'nonce_anchor': 1,
            'nonce_bitmap_index': 2,
            'deadline': DEADLINE,
            'signature': SIGNATURE,
        },
    }


def _evidence(*, used: bool = True, consistent: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        block_number=54750000,
        block_tag=hex(54750000),
        nonce_anchor=1,
        nonce_bitmap_index=2,
        is_nonce_used=used,
        state_anchor=1,
        state_bitmap=0x7 if consistent else 0x3,
        bitmap_consistent=consistent,
    )


class FakeTransport:
    def __init__(self, *, payloads=None, outcomes=None):
        base = _payload()
        self.payloads = payloads or [copy.deepcopy(base), copy.deepcopy(base)]
        self.outcomes = outcomes or [
            {'success': True, 'order_id': 'first'},
            {'success': True, 'order_id': 'second'},
        ]
        self.prepare_calls = 0
        self.prepared_requests = []
        self.post_calls = []

    async def prepare_place_order_post(self, request):
        index = self.prepare_calls
        self.prepare_calls += 1
        self.prepared_requests.append(request)
        return copy.deepcopy(self.payloads[index])

    async def post_prepared_place_order(self, payload):
        self.post_calls.append(copy.deepcopy(payload))
        outcome = self.outcomes[len(self.post_calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _wrapped_http_failure(status_code, body, *, timeout=False):
    request = httpx.Request('POST', 'https://api.testnet.rise.trade/v1/orders/place')
    if timeout:
        cause = httpx.ReadTimeout('sentinel timeout', request=request)
    else:
        response = httpx.Response(status_code, json=body, request=request)
        cause = httpx.HTTPStatusError(
            f'{status_code} sentinel',
            request=request,
            response=response,
        )
    try:
        raise cause
    except BaseException as root:
        try:
            raise SignedTestnetBlocked('wrapped provider failure') from root
        except SignedTestnetBlocked as wrapped:
            return wrapped


async def _run(module, *, transport, evidence=None, precondition_error=None):
    collect_consumed = AsyncMock(return_value=evidence or _evidence())

    async def validate_second_preconditions(**_kwargs):
        if precondition_error is not None:
            raise precondition_error
        return SimpleNamespace(
            deadline_margin_seconds=30,
            deployment_unchanged=True,
            session_active=True,
            identity_unchanged=True,
        )

    result = await module._execute_replay_sequence(
        request=_request(),
        transport=transport,
        collect_consumed_nonce=collect_consumed,
        validate_second_preconditions=validate_second_preconditions,
    )
    assert isinstance(result, dict)
    return result


def test_01_dedicated_approval_flag_absent_means_zero_post(monkeypatch):
    module = _module()
    execute_once = AsyncMock(side_effect=AssertionError('must not execute'))
    monkeypatch.setattr(module, '_execute_once', execute_once)
    parser = module.build_parser()
    options = {o for action in parser._actions for o in action.option_strings}

    exit_code = module.main([])

    assert '--approve-two-identical-signed-order-submissions' in options
    assert exit_code != 0
    assert execute_once.await_count == 0


def test_02_old_manual_approval_flag_alone_cannot_authorize(monkeypatch):
    module = _module()
    execute_once = AsyncMock(side_effect=AssertionError('must not execute'))
    monkeypatch.setattr(module, '_execute_once', execute_once)

    try:
        exit_code = module.main(['--approve-testnet-order'])
    except SystemExit as exc:
        exit_code = int(exc.code or 0)

    assert exit_code != 0
    assert execute_once.await_count == 0


def test_03_non_testnet_runtime_is_rejected_before_any_post():
    module = _module()

    with pytest.raises(SignedTestnetBlocked, match='testnet|chain'):
        module._assert_testnet_runtime(
            network='mainnet',
            chain_id=1,
            api_base_url='https://api.rise.trade',
        )


def test_04_request_preparation_occurs_exactly_once():
    module = _module()
    source = inspect.getsource(module._execute_once)
    assert source.count('prepare_risex_ioc_request(') == 1


def test_05_replay_architecture_collector_occurs_exactly_once():
    module = _module()
    source = inspect.getsource(module._execute_once)
    assert source.count('collect_replay_protection_architecture_attestation(') == 1


def test_06_order_intent_is_provider_minimum_size():
    module = _module()
    intent = module.negative_replay_probe_intent()
    assert intent.symbol == 'BTC'
    assert intent.side == 'BUY'
    assert intent.use_min_order_size is True


def test_07_deadline_is_fixed_and_not_cli_configurable():
    module = _module()
    parser = module.build_parser()
    options = {o for action in parser._actions for o in action.option_strings}
    assert module.REPLAY_DEADLINE_SECONDS == 60
    assert module.MIN_REPLAY_DEADLINE_MARGIN_SECONDS == 15
    assert '--deadline-seconds' not in options


@pytest.mark.asyncio
async def test_08_budget_is_two_and_failed_post_is_already_consumed():
    module = _module()
    transport = FakeTransport(outcomes=[_wrapped_http_failure(400, {'message': 'rejected'})])
    result = await _run(module, transport=transport)
    assert module.MAX_SUBMISSIONS == 2
    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1


@pytest.mark.asyncio
async def test_09_first_explicit_failure_causes_exactly_one_post():
    module = _module()
    transport = FakeTransport(outcomes=[_wrapped_http_failure(400, {'message': 'rejected'})])
    result = await _run(module, transport=transport)
    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1


@pytest.mark.asyncio
async def test_10_first_ambiguous_failure_causes_exactly_one_post():
    module = _module()
    transport = FakeTransport(outcomes=[_wrapped_http_failure(0, {}, timeout=True)])
    result = await _run(module, transport=transport)
    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1


@pytest.mark.asyncio
async def test_11_first_accepted_but_nonce_not_consumed_causes_one_post():
    module = _module()
    transport = FakeTransport()
    result = await _run(module, transport=transport, evidence=_evidence(used=False))
    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1
    assert result['result'] == 'NONCE_CONSUMPTION_INCONSISTENT'


@pytest.mark.asyncio
async def test_12_inconsistent_nonce_used_and_bitmap_causes_one_post():
    module = _module()
    transport = FakeTransport()
    result = await _run(module, transport=transport, evidence=_evidence(consistent=False))
    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1
    assert result['result'] == 'NONCE_CONSUMPTION_INCONSISTENT'


@pytest.mark.asyncio
async def test_13_insufficient_deadline_margin_causes_one_post():
    module = _module()
    transport = FakeTransport()
    result = await _run(
        module,
        transport=transport,
        precondition_error=SignedTestnetBlocked('DEADLINE_WINDOW_LOST'),
    )
    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1
    assert result['result'] == 'DEADLINE_WINDOW_LOST'


@pytest.mark.asyncio
async def test_14_payload_fingerprint_mismatch_blocks_second_post():
    module = _module()
    first = _payload()
    second = copy.deepcopy(first)
    second['client_order_id'] = str(CLIENT_ORDER_ID + 1)
    transport = FakeTransport(payloads=[first, second])

    result = await _run(module, transport=transport)

    assert len(transport.post_calls) == 1
    assert result['submission_count'] == 1
    assert result['result'] == 'PAYLOAD_IDENTITY_MISMATCH'


@pytest.mark.asyncio
async def test_15_valid_path_sends_exactly_two_posts():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        _wrapped_http_failure(400, {'message': 'request rejected'}),
    ])
    result = await _run(module, transport=transport)
    assert len(transport.post_calls) == 2
    assert result['submission_count'] == 2


@pytest.mark.asyncio
async def test_16_two_submitted_payloads_are_mechanically_identical():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        _wrapped_http_failure(400, {'message': 'request rejected'}),
    ])
    result = await _run(module, transport=transport)

    assert len(transport.post_calls) == 2
    canonical = lambda p: json.dumps(p, sort_keys=True, separators=(',', ':'))
    assert canonical(transport.post_calls[0]) == canonical(transport.post_calls[1])
    assert result['first_payload_fingerprint'] == result['second_payload_fingerprint']


@pytest.mark.asyncio
async def test_17_signature_nonce_deadline_action_hash_and_client_order_id_are_identical():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        _wrapped_http_failure(400, {'message': 'request rejected'}),
    ])
    await _run(module, transport=transport)

    first, second = transport.post_calls
    first_permit, second_permit = first['permit'], second['permit']
    for field in ('signature', 'nonce_anchor', 'nonce_bitmap_index', 'deadline'):
        assert first_permit[field] == second_permit[field]
    assert first['client_order_id'] == second['client_order_id']
    assert len(transport.prepared_requests) == 2
    assert transport.prepared_requests[0] is transport.prepared_requests[1]
    request = transport.prepared_requests[0]
    assert request.permit.action_hash == ACTION_HASH
    assert request.order.client_order_id == CLIENT_ORDER_ID


@pytest.mark.asyncio
async def test_18_nonce_attributable_second_rejection_is_nonce_classification():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        _wrapped_http_failure(
            409,
            {'code': 'NONCE_ALREADY_USED', 'message': 'permit nonce already used'},
        ),
    ])
    result = await _run(module, transport=transport)
    assert result['result'] == 'REPLAY_REJECTED_NONCE'
    assert result['behavioral_replay_rejection_proven'] is True


@pytest.mark.asyncio
async def test_19_unattributed_second_rejection_is_unspecified():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        _wrapped_http_failure(400, {'message': 'request rejected'}),
    ])
    result = await _run(module, transport=transport)
    assert result['result'] == 'REPLAY_REJECTED_UNSPECIFIED'
    assert result['behavioral_replay_rejection_proven'] is False


def test_20_only_nonce_attributable_rejection_is_eligible_behavioral_proof():
    module = _module()
    assert module.behavioral_replay_rejection_proven_for('REPLAY_REJECTED_NONCE') is True
    for result in (
        'REPLAY_REJECTED_UNSPECIFIED',
        'SECOND_SUBMISSION_AMBIGUOUS',
        'REPLAY_ACCEPTED_SECURITY_FAILURE',
        'NONCE_CONSUMPTION_INCONSISTENT',
        'DEADLINE_WINDOW_LOST',
        'PAYLOAD_IDENTITY_MISMATCH',
    ):
        assert module.behavioral_replay_rejection_proven_for(result) is False


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    _wrapped_http_failure(503, {'message': 'temporary server failure'}),
    _wrapped_http_failure(0, {}, timeout=True),
])
async def test_21_second_5xx_or_timeout_is_ambiguous_and_never_retried(failure):
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        failure,
    ])
    result = await _run(module, transport=transport)
    assert result['result'] == 'SECOND_SUBMISSION_AMBIGUOUS'
    assert len(transport.post_calls) == 2
    assert result['submission_count'] == 2


@pytest.mark.asyncio
async def test_22_second_accepted_submission_is_security_failure():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        {'success': True, 'order_id': 'second', 'tx_hash': '0xdeadbeef'},
    ])
    result = await _run(module, transport=transport)
    assert result['result'] == 'REPLAY_ACCEPTED_SECURITY_FAILURE'
    assert result['behavioral_replay_rejection_proven'] is False


@pytest.mark.asyncio
async def test_23_second_acceptance_never_causes_third_post():
    module = _module()
    transport = FakeTransport(outcomes=[
        {'success': True, 'order_id': 'first'},
        {'success': True, 'order_id': 'second'},
        AssertionError('third POST must be impossible'),
    ])
    result = await _run(module, transport=transport)
    assert result['result'] == 'REPLAY_ACCEPTED_SECURITY_FAILURE'
    assert len(transport.post_calls) == 2
    assert result['submission_count'] == 2


def test_24_output_sanitization_removes_permit_and_signature():
    module = _module()
    sanitized = module.sanitize_diagnostic_payload({
        'result': 'REPLAY_REJECTED_UNSPECIFIED',
        'permit': _payload()['permit'],
        'signature': SIGNATURE,
        '_signature': SIGNATURE,
        'payload_fingerprint': 'abc123',
    })
    rendered = json.dumps(sanitized, sort_keys=True).lower()
    assert 'permit' not in rendered
    assert 'signature' not in rendered
    assert SIGNATURE.lower() not in rendered
    assert sanitized['payload_fingerprint'] == 'abc123'


def test_25_output_sanitization_removes_private_key_and_environment_secrets():
    module = _module()
    sanitized = module.sanitize_diagnostic_payload({
        'result': 'REPLAY_REJECTED_UNSPECIFIED',
        'RISEX_TESTNET_SIGNER_PRIVATE_KEY': PRIVATE_KEY,
        'private_key': PRIVATE_KEY,
        'secret': 'hidden',
        'cookie': 'session=hidden',
        'authorization': 'Bearer hidden',
        'payload_fingerprint': 'abc123',
    })
    rendered = json.dumps(sanitized, sort_keys=True).lower()
    assert PRIVATE_KEY.lower() not in rendered
    assert 'private_key' not in rendered
    assert 'secret' not in rendered
    assert 'cookie' not in rendered
    assert 'authorization' not in rendered
    assert sanitized['payload_fingerprint'] == 'abc123'
