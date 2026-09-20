from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import secrets
from typing import Any, Literal

from app.adapters.risex_types import (
    ProviderDataMalformed,
    ProviderReadUnavailable,
    RISExPublicReadTransport,
)
from app.security.risex_authorization_session import collect_authorization_session_evidence
from app.security.risex_deployment_runtime import (
    PublicAPITransport,
    PublicRPCTransport,
    collect_runtime_deployment_evidence,
)
from app.security.risex_nonce_state import collect_order_nonce_selection
from app.security.risex_order_codec import RISExPlaceOrder
from app.security.risex_place_order_permit import prepare_place_order_permit
from app.security.risex_place_order_request import (
    RISExPreparedPlaceOrderRequest,
    prepare_place_order_request,
)
from app.security.risex_signed_testnet_policy import SignedTestnetBlocked
from app.security.risex_signer_probe import RISExSignerCapabilityEvidence
from app.security.risex_testnet_signer import load_testnet_signer_credential


Side = Literal['BUY', 'SELL']
Network = Literal['testnet', 'mainnet']
_UINT32_LIMIT = 1 << 32
_UINT24_LIMIT = 1 << 24
_UINT64_LIMIT = 1 << 64


@dataclass(frozen=True, slots=True)
class RISExMarketMetadata:
    market_id: int
    step_size: Decimal
    step_price: Decimal
    min_order_size: Decimal


@dataclass(frozen=True, slots=True)
class RISExTopOfBook:
    best_bid: Decimal
    best_ask: Decimal


@dataclass(frozen=True, slots=True)
class RISExOrderIntent:
    symbol: str
    is_buy: bool
    requested_size: Decimal
    reduce_only: bool
    slippage_bps: int
    client_order_id: int


@dataclass(frozen=True, slots=True)
class RISExIOCPlan:
    order: RISExPlaceOrder
    requested_size: Decimal
    limit_price: Decimal
    market: RISExMarketMetadata


def _unwrap_data(payload: dict[str, Any], *, field: str) -> dict[str, Any]:
    data = payload.get('data')
    if data is None:
        return payload
    if not isinstance(data, dict):
        raise ProviderDataMalformed(f'RISEx {field} data envelope is not an object')
    return data


def _positive_decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ProviderDataMalformed(f'RISEx {field} is not a decimal value')
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ProviderDataMalformed(f'RISEx {field} is not a decimal value') from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ProviderDataMalformed(f'RISEx {field} must be positive and finite')
    return parsed


def _positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise ProviderDataMalformed(f'RISEx {field} is not an integer')
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ProviderDataMalformed(f'RISEx {field} is not an integer') from exc
    if parsed <= 0:
        raise ProviderDataMalformed(f'RISEx {field} must be positive')
    return parsed


def _symbol_matches(row: dict[str, Any], symbol: str) -> bool:
    target = symbol.strip().upper()
    config = row.get('config')
    config_name = config.get('name') if isinstance(config, dict) else None
    candidates = (
        row.get('base_asset_symbol'),
        row.get('display_base_asset_symbol'),
        row.get('display_name'),
        row.get('underlying'),
        config_name,
    )
    for value in candidates:
        if not isinstance(value, str):
            continue
        normalized = value.strip().upper()
        base = normalized.split('/', 1)[0]
        if normalized == target or base == target:
            return True
    return False


async def resolve_market_metadata(
    api: RISExPublicReadTransport,
    *,
    symbol: str,
) -> RISExMarketMetadata:
    """Resolve tradable market precision and minimum size from live RISEx metadata."""

    if getattr(api, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx market metadata resolution requires a public read-only transport'
        )

    payload = _unwrap_data(await api.get_json('/v1/markets'), field='markets')
    markets = payload.get('markets')
    if not isinstance(markets, list):
        raise ProviderDataMalformed('RISEx markets response has no markets list')

    for raw in markets:
        if not isinstance(raw, dict) or not _symbol_matches(raw, symbol):
            continue
        if raw.get('active') is False:
            continue

        config = raw.get('config')
        if not isinstance(config, dict):
            raise ProviderDataMalformed('RISEx market config is missing')
        if config.get('unlocked') is False:
            continue

        market_id = _positive_int(raw.get('market_id'), field='market_id')
        if market_id >= (1 << 16):
            raise ProviderDataMalformed('RISEx market_id exceeds uint16')

        return RISExMarketMetadata(
            market_id=market_id,
            step_size=_positive_decimal(config.get('step_size'), field='step_size'),
            step_price=_positive_decimal(config.get('step_price'), field='step_price'),
            min_order_size=_positive_decimal(
                config.get('min_order_size'),
                field='min_order_size',
            ),
        )

    raise ProviderDataMalformed(f'RISEx active unlocked market for {symbol!r} was not found')


async def load_top_of_book(
    api: RISExPublicReadTransport,
    *,
    market_id: int,
) -> RISExTopOfBook:
    """Read one live best bid and ask using provider decimal-price fields."""

    if getattr(api, 'public_read_only', False) is not True:
        raise ProviderReadUnavailable(
            'RISEx orderbook resolution requires a public read-only transport'
        )
    payload = _unwrap_data(
        await api.get_json(
            '/v1/orderbook',
            params={'market_id': market_id, 'limit': 1},
        ),
        field='orderbook',
    )
    bids = payload.get('bids')
    asks = payload.get('asks')
    if not isinstance(bids, list) or not bids or not isinstance(asks, list) or not asks:
        raise ProviderDataMalformed('RISEx orderbook has no usable best bid/ask')

    best_bid_row = bids[0]
    best_ask_row = asks[0]
    if not isinstance(best_bid_row, dict) or not isinstance(best_ask_row, dict):
        raise ProviderDataMalformed('RISEx orderbook top levels are malformed')

    best_bid = _positive_decimal(best_bid_row.get('price'), field='best bid price')
    best_ask = _positive_decimal(best_ask_row.get('price'), field='best ask price')
    if best_bid > best_ask:
        raise ProviderDataMalformed('RISEx orderbook is crossed')

    return RISExTopOfBook(best_bid=best_bid, best_ask=best_ask)


def size_to_steps(
    *,
    size: Decimal,
    step_size: Decimal,
    min_order_size: Decimal,
) -> int:
    """Convert a requested decimal quantity into uint32 protocol steps, rounding up."""

    if size <= 0 or step_size <= 0 or min_order_size <= 0:
        raise SignedTestnetBlocked('RISEx size and market sizing metadata must be positive')
    effective_size = max(size, min_order_size)
    steps = int(
        (effective_size / step_size).to_integral_value(rounding=ROUND_CEILING)
    )
    if steps <= 0 or steps >= _UINT32_LIMIT:
        raise SignedTestnetBlocked('RISEx size_steps is outside uint32 range')
    return steps


def marketable_price_to_ticks(
    *,
    side: Side,
    best_bid: Decimal,
    best_ask: Decimal,
    step_price: Decimal,
    slippage_bps: int,
) -> int:
    """Build a marketable IOC limit and convert it into protocol ticks.

    BUY limits are rounded upward so tick conversion never makes the limit less
    marketable. SELL limits are rounded downward for the symmetric reason.
    """

    if side not in {'BUY', 'SELL'}:
        raise SignedTestnetBlocked('RISEx side must be BUY or SELL')
    if type(slippage_bps) is not int or not 0 <= slippage_bps < 10_000:
        raise SignedTestnetBlocked('RISEx slippage_bps must be an integer in [0, 9999]')
    if best_bid <= 0 or best_ask <= 0 or step_price <= 0:
        raise SignedTestnetBlocked('RISEx orderbook prices and step_price must be positive')
    if best_bid > best_ask:
        raise SignedTestnetBlocked('RISEx orderbook is crossed')

    slippage = Decimal(slippage_bps) / Decimal(10_000)
    if side == 'BUY':
        target = best_ask * (Decimal(1) + slippage)
        rounding = ROUND_CEILING
    else:
        target = best_bid * (Decimal(1) - slippage)
        rounding = ROUND_FLOOR

    if target <= 0:
        raise SignedTestnetBlocked('RISEx marketable limit must remain positive')
    ticks = int((target / step_price).to_integral_value(rounding=rounding))
    if ticks <= 0 or ticks >= _UINT24_LIMIT:
        raise SignedTestnetBlocked('RISEx price_ticks is outside uint24 range')
    return ticks


def assert_risex_execution_network_allowed(network: Network = 'testnet') -> Network:
    """Fail closed until ADR-0006 is Accepted by the mainnet-enablement PR."""

    if network == 'testnet':
        return network
    if network == 'mainnet':
        raise RuntimeError('RISEx mainnet is blocked until ADR-0006 is Accepted')
    raise RuntimeError(f'Unsupported RISEx execution network: {network!r}')


def generate_client_order_id() -> int:
    """Generate a non-zero uint64 client order id for manual/probe tooling only."""

    value = 0
    while value == 0:
        value = secrets.randbits(64)
    return value


async def prepare_risex_ioc_plan(
    api: RISExPublicReadTransport,
    intent: RISExOrderIntent,
) -> RISExIOCPlan:
    """Build a provider-typed unsigned IOC plan from application intent."""

    market = await resolve_market_metadata(api, symbol=intent.symbol)
    book = await load_top_of_book(api, market_id=market.market_id)
    requested_size = Decimal(str(intent.requested_size))
    size_steps = size_to_steps(
        size=requested_size,
        step_size=market.step_size,
        min_order_size=market.min_order_size,
    )
    side: Side = 'BUY' if intent.is_buy else 'SELL'
    price_ticks = marketable_price_to_ticks(
        side=side,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        step_price=market.step_price,
        slippage_bps=intent.slippage_bps,
    )
    if type(intent.client_order_id) is not int or not 0 < intent.client_order_id < _UINT64_LIMIT:
        raise SignedTestnetBlocked('RISEx client_order_id must be a non-zero uint64 integer')

    order = RISExPlaceOrder(
        market_id=market.market_id,
        size_steps=size_steps,
        price_ticks=price_ticks,
        side=0 if intent.is_buy else 1,
        post_only=False,
        reduce_only=intent.reduce_only,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=intent.client_order_id,
        ttl_units=0,
    )
    return RISExIOCPlan(
        order=order,
        requested_size=Decimal(size_steps) * market.step_size,
        limit_price=Decimal(price_ticks) * market.step_price,
        market=market,
    )


def _deadline(
    *,
    observed_block_timestamp: int,
    session_expiration: int,
    deadline_seconds: int,
) -> int:
    if type(deadline_seconds) is not int or deadline_seconds <= 0:
        raise SignedTestnetBlocked('RISEx deadline_seconds must be a positive integer')
    if session_expiration <= observed_block_timestamp + 1:
        raise SignedTestnetBlocked('RISEx signer session has no safe permit-deadline window')

    deadline = min(
        observed_block_timestamp + deadline_seconds,
        session_expiration - 1,
    )
    if deadline <= observed_block_timestamp:
        raise SignedTestnetBlocked('RISEx permit deadline must be after the observed block')
    return deadline


async def prepare_risex_ioc_request(
    *,
    env: Mapping[str, str],
    api: PublicAPITransport,
    rpc: PublicRPCTransport,
    network: Network = 'testnet',
    symbol: str,
    side: Side,
    use_min_order_size: bool,
    size: Decimal | None = None,
    slippage_bps: int = 25,
    deadline_seconds: int = 30,
    client_order_id_factory: Callable[[], int] = generate_client_order_id,
) -> RISExPreparedPlaceOrderRequest:
    """Prepare one typed, signed RISEx IOC request without provider mutation.

    All market sizing/price metadata, runtime EIP-712 identity and nonce state are
    collected live. The nonce read is deliberately the final provider read before
    permit signing and typed request binding.
    """

    network = assert_risex_execution_network_allowed(network)
    credential = load_testnet_signer_credential(env)
    market = await resolve_market_metadata(api, symbol=symbol)
    book = await load_top_of_book(api, market_id=market.market_id)

    deployment = await collect_runtime_deployment_evidence(
        api,
        rpc,
        network=network,
    )
    required_runtime = (
        deployment.block_number,
        deployment.api_chain_id,
        deployment.domain_name,
        deployment.domain_version,
        deployment.domain_verifying_contract,
        deployment.system_router,
    )
    if any(value is None for value in required_runtime):
        raise SignedTestnetBlocked('RISEx runtime EIP-712 identity is incomplete')

    block_number = int(deployment.block_number)  # type: ignore[arg-type]
    chain_id = int(deployment.api_chain_id)  # type: ignore[arg-type]
    domain_name = str(deployment.domain_name)
    domain_version = str(deployment.domain_version)
    verifying_contract = str(deployment.domain_verifying_contract)
    router = str(deployment.system_router)

    authorization = await collect_authorization_session_evidence(
        rpc,
        authorization_address=verifying_contract,
        account=credential.account_address,
        signer=credential.signer_address,
        block_tag=hex(block_number),
    )

    if use_min_order_size:
        requested_size = market.min_order_size
    else:
        if size is None:
            raise SignedTestnetBlocked(
                'RISEx explicit size is required when minimum-size mode is disabled'
            )
        requested_size = size

    size_steps = size_to_steps(
        size=requested_size,
        step_size=market.step_size,
        min_order_size=market.min_order_size,
    )
    price_ticks = marketable_price_to_ticks(
        side=side,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        step_price=market.step_price,
        slippage_bps=slippage_bps,
    )

    client_order_id = client_order_id_factory()
    if type(client_order_id) is not int or not 0 < client_order_id < _UINT64_LIMIT:
        raise SignedTestnetBlocked(
            'RISEx client_order_id must be a non-zero uint64 integer'
        )

    order = RISExPlaceOrder(
        market_id=market.market_id,
        size_steps=size_steps,
        price_ticks=price_ticks,
        side=0 if side == 'BUY' else 1,
        post_only=False,
        reduce_only=False,
        stp_mode=0,
        order_type=1,
        time_in_force=3,
        client_order_id=client_order_id,
        ttl_units=0,
    )

    # Security ordering invariant: provider nonce is selected immediately before
    # signing. No provider/network read belongs between this call and permit signing.
    nonce_selection = await collect_order_nonce_selection(
        api,
        account=credential.account_address,
    )
    permit = prepare_place_order_permit(
        order=order,
        credential=credential,
        nonce_selection=nonce_selection,
        domain_name=domain_name,
        domain_version=domain_version,
        chain_id=chain_id,
        verifying_contract=verifying_contract,
        router=router,
        observed_block_timestamp=authorization.block_timestamp,
        session_expiration=authorization.session_expiration,
        deadline=_deadline(
            observed_block_timestamp=authorization.block_timestamp,
            session_expiration=authorization.session_expiration,
            deadline_seconds=deadline_seconds,
        ),
    )
    return prepare_place_order_request(order=order, permit=permit)


def make_freshness_probe(
    *,
    api: RISExPublicReadTransport,
    rpc: PublicRPCTransport,
    account_address: str,
    signer_address: str,
    operatorhub_bypass_disabled: bool,
    fund_movement_path_absent: bool,
) -> Callable[[], Any]:
    """Create a no-cache freshness probe from live deployment + on-chain session state.

    Every invocation recollects the runtime deployment identity, including a fresh
    block number, then reads the Authorization session at that exact block. No
    block tag or session lifecycle evidence is captured when the callback is built.
    """

    async def probe() -> Any:
        deployment = await collect_runtime_deployment_evidence(
            api,
            rpc,
            network='testnet',
        )
        required_runtime = (
            deployment.block_number,
            deployment.api_chain_id,
            deployment.domain_verifying_contract,
            deployment.system_router,
        )
        if any(value is None for value in required_runtime):
            raise SignedTestnetBlocked(
                'RISEx freshness deployment evidence is incomplete'
            )

        block_number = int(deployment.block_number)  # type: ignore[arg-type]
        authorization_address = str(deployment.domain_verifying_contract)
        authorization = await collect_authorization_session_evidence(
            rpc,
            authorization_address=authorization_address,
            account=account_address,
            signer=signer_address,
            block_tag=hex(block_number),
        )

        return RISExSignerCapabilityEvidence(
            network='testnet',
            account=account_address,
            signer=signer_address,
            chain_id=int(deployment.api_chain_id),  # type: ignore[arg-type]
            auth_contract=authorization_address,
            router=str(deployment.system_router),
            session_active=authorization.session_active,
            session_account=authorization.account,
            session_expiration=authorization.session_expiration,
            onchain_perps_only_scope=authorization.perps_only_scope,
            perps_order_succeeded=None,
            fund_movement_rejected=None,
            withdrawal_rejected=None,
            post_revoke_order_rejected=None,
            operatorhub_bypass_disabled=operatorhub_bypass_disabled,
            perps_permission=authorization.perps_permission,
            fund_movement_path_absent=fund_movement_path_absent,
        )

    return probe
