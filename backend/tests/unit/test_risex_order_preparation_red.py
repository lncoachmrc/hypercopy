from __future__ import annotations

from decimal import Decimal
import importlib
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from app.security.risex_place_order_request import RISExPreparedPlaceOrderRequest


ACCOUNT = '0x' + ('11' * 20)
SIGNER = '0x' + ('33' * 20)
AUTH = '0x' + ('aa' * 20)
ROUTER = '0x' + ('bb' * 20)
SIGNER_PRIVATE_KEY = '0x' + ('22' * 32)


class FakeAPI:
    public_read_only = True

    def __init__(self, payloads: dict[str, dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        try:
            return self.payloads[path]
        except KeyError as exc:
            raise AssertionError(f'unexpected RISEx GET: {path}') from exc


def _module() -> ModuleType:
    try:
        return importlib.import_module('app.services.risex_order_preparation')
    except ModuleNotFoundError as exc:
        pytest.fail(
            'RED: reusable RISEx order preparation module is not implemented yet: '
            f'{exc}',
            pytrace=False,
        )


def _market_payload() -> dict[str, Any]:
    return {
        'data': {
            'markets': [
                {
                    'market_id': '17',
                    'display_name': 'BTC/USDC',
                    'base_asset_symbol': 'BTC',
                    'quote_asset_symbol': 'USDC',
                    'config': {
                        'step_size': '0.000001',
                        'step_price': '0.1',
                        'min_order_size': '0.00015',
                    },
                },
                {
                    'market_id': '22',
                    'display_name': 'ETH/USDC',
                    'base_asset_symbol': 'ETH',
                    'quote_asset_symbol': 'USDC',
                    'config': {
                        'step_size': '0.0001',
                        'step_price': '0.01',
                        'min_order_size': '0.01',
                    },
                },
            ]
        }
    }


def _book_payload() -> dict[str, Any]:
    return {
        'data': {
            'market_id': 17,
            'bids': [{'price': '63120.05', 'quantity': '1.2'}],
            'asks': [{'price': '63123.45', 'quantity': '0.9'}],
        }
    }


@pytest.mark.asyncio
async def test_market_metadata_is_resolved_live_and_not_hardcoded() -> None:
    module = _module()
    api = FakeAPI({'/v1/markets': _market_payload()})

    market = await module.resolve_market_metadata(api, symbol='BTC')

    assert api.calls == [('/v1/markets', None)]
    assert market.market_id == 17
    assert market.step_size == Decimal('0.000001')
    assert market.step_price == Decimal('0.1')
    assert market.min_order_size == Decimal('0.00015')


@pytest.mark.asyncio
async def test_top_of_book_uses_live_orderbook_endpoint() -> None:
    module = _module()
    api = FakeAPI({'/v1/orderbook': _book_payload()})

    book = await module.load_top_of_book(api, market_id=17)

    assert api.calls == [('/v1/orderbook', {'market_id': 17, 'limit': 1})]
    assert book.best_bid == Decimal('63120.05')
    assert book.best_ask == Decimal('63123.45')


def test_minimum_size_and_marketable_prices_use_directional_rounding() -> None:
    module = _module()

    size_steps = module.size_to_steps(
        size=Decimal('0.00015'),
        step_size=Decimal('0.000001'),
        min_order_size=Decimal('0.00015'),
    )
    buy_ticks = module.marketable_price_to_ticks(
        side='BUY',
        best_bid=Decimal('63120.05'),
        best_ask=Decimal('63123.45'),
        step_price=Decimal('0.1'),
        slippage_bps=25,
    )
    sell_ticks = module.marketable_price_to_ticks(
        side='SELL',
        best_bid=Decimal('63120.05'),
        best_ask=Decimal('63123.45'),
        step_price=Decimal('0.1'),
        slippage_bps=25,
    )

    assert size_steps == 150
    assert buy_ticks == 632813
    assert sell_ticks == 629622


def test_client_order_id_is_unique_nonzero_uint64() -> None:
    module = _module()

    identifiers = [module.generate_client_order_id() for _ in range(64)]

    assert len(set(identifiers)) == len(identifiers)
    assert all(type(value) is int for value in identifiers)
    assert all(0 < value < (1 << 64) for value in identifiers)


@pytest.mark.asyncio
async def test_preparer_builds_typed_request_from_live_metadata_runtime_and_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    api = FakeAPI(
        {
            '/v1/markets': _market_payload(),
            '/v1/orderbook': _book_payload(),
            f'/v1/nonce-state/{ACCOUNT}': {
                'data': {
                    'nonce_anchor': '7',
                    'current_bitmap_index': 3,
                }
            },
        }
    )

    async def collect_deployment(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            block_number=0x1234,
            api_chain_id=11155931,
            rpc_chain_id=11155931,
            domain_name='RISEx Runtime Domain',
            domain_version='9',
            domain_verifying_contract=AUTH,
            system_auth_contract=AUTH,
            system_router=ROUTER,
        )

    async def collect_authorization(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            block_timestamp=1_900_000_000,
            session_expiration=1_900_000_015,
        )

    monkeypatch.setattr(module, 'collect_runtime_deployment_evidence', collect_deployment)
    monkeypatch.setattr(
        module,
        'collect_authorization_session_evidence',
        collect_authorization,
    )

    request = await module.prepare_risex_ioc_request(
        env={
            'RISEX_TESTNET_ACCOUNT_ADDRESS': ACCOUNT,
            'RISEX_TESTNET_SIGNER_PRIVATE_KEY': SIGNER_PRIVATE_KEY,
        },
        api=api,
        rpc=object(),
        symbol='BTC',
        side='BUY',
        use_min_order_size=True,
        slippage_bps=25,
        deadline_seconds=30,
        client_order_id_factory=lambda: 42,
    )

    assert isinstance(request, RISExPreparedPlaceOrderRequest)
    assert request.order.market_id == 17
    assert request.order.size_steps == 150
    assert request.order.price_ticks == 632813
    assert request.order.side == 0
    assert request.order.order_type == 1
    assert request.order.time_in_force == 3
    assert request.order.client_order_id == 42
    assert request.permit.account_address == ACCOUNT
    assert request.permit.nonce_anchor == 7
    assert request.permit.nonce_bitmap_index == 3
    assert 1_900_000_000 < request.permit.deadline < 1_900_000_015
    assert request.post_allowed is False


@pytest.mark.asyncio
async def test_nonce_is_selected_immediately_before_permit_signing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    events: list[str] = []
    sentinel = object()

    async def resolve_market(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append('market')
        return SimpleNamespace(
            market_id=17,
            step_size=Decimal('0.000001'),
            step_price=Decimal('0.1'),
            min_order_size=Decimal('0.00015'),
        )

    async def load_book(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append('book')
        return SimpleNamespace(
            best_bid=Decimal('63120.05'),
            best_ask=Decimal('63123.45'),
        )

    async def collect_deployment(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append('runtime')
        return SimpleNamespace(
            block_number=0x1234,
            api_chain_id=11155931,
            domain_name='RISEx Runtime Domain',
            domain_version='9',
            domain_verifying_contract=AUTH,
            system_router=ROUTER,
        )

    async def collect_authorization(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append('authorization')
        return SimpleNamespace(
            block_timestamp=1_900_000_000,
            session_expiration=1_900_000_100,
        )

    async def collect_nonce(*_args: object, **_kwargs: object) -> SimpleNamespace:
        events.append('nonce')
        return SimpleNamespace(
            observed_nonce_anchor=7,
            observed_bitmap_index=3,
            selected_nonce_anchor=7,
            selected_bitmap_index=3,
            rolled_anchor=False,
        )

    def sign_permit(**_kwargs: object) -> object:
        events.append('sign')
        return object()

    def bind_request(**_kwargs: object) -> object:
        events.append('bind')
        return sentinel

    monkeypatch.setattr(module, 'resolve_market_metadata', resolve_market)
    monkeypatch.setattr(module, 'load_top_of_book', load_book)
    monkeypatch.setattr(module, 'collect_runtime_deployment_evidence', collect_deployment)
    monkeypatch.setattr(
        module,
        'collect_authorization_session_evidence',
        collect_authorization,
    )
    monkeypatch.setattr(module, 'collect_order_nonce_selection', collect_nonce)
    monkeypatch.setattr(module, 'prepare_place_order_permit', sign_permit)
    monkeypatch.setattr(module, 'prepare_place_order_request', bind_request)
    monkeypatch.setattr(
        module,
        'load_testnet_signer_credential',
        lambda _env: SimpleNamespace(
            account_address=ACCOUNT,
            signer_address=SIGNER,
        ),
    )

    result = await module.prepare_risex_ioc_request(
        env={},
        api=object(),
        rpc=object(),
        symbol='BTC',
        side='BUY',
        use_min_order_size=True,
        slippage_bps=25,
        deadline_seconds=30,
        client_order_id_factory=lambda: 42,
    )

    assert result is sentinel
    assert events[-3:] == ['nonce', 'sign', 'bind']


@pytest.mark.asyncio
async def test_freshness_probe_recollects_live_evidence_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[tuple[str, str]] = []
    first = object()
    second = object()
    results = iter((first, second))

    async def collect(
        _api: object,
        *,
        network: str,
        account: str,
        signer: str,
    ) -> object:
        assert network == 'testnet'
        calls.append((account, signer))
        return next(results)

    monkeypatch.setattr(module, 'collect_public_signer_evidence', collect)

    probe = module.make_freshness_probe(
        api=object(),
        account_address=ACCOUNT,
        signer_address=SIGNER,
    )

    assert await probe() is first
    assert await probe() is second
    assert calls == [(ACCOUNT, SIGNER), (ACCOUNT, SIGNER)]
