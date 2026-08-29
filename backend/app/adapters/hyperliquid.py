from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator, Awaitable, Callable

import websockets
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid, Meta

from app.adapters.address_ratelimit import AddressActionTracker, is_exchange_rate_limit_error
from app.core.config import Network, settings
from app.core.logging import get_logger
from app.db.signer_action_lock import signer_action_lock
from app.engine.sizing import AssetSpec, round_price
from app.adapters.ratelimit import Priority, WEIGHT_CHEAP_INFO, WEIGHT_EXCHANGE_ACTION, WEIGHT_STANDARD_INFO, WEIGHT_USER_FILLS_MAX, WeightedRateLimiter

log = get_logger(__name__)

_EMPTY_PERP_META = {'universe': []}
_EMPTY_SPOT_META = {'universe': [], 'tokens': []}


@dataclass(frozen=True, slots=True)
class AgentVerification:
    agent_address: str
    name: str
    valid_until: int | None


@dataclass(frozen=True, slots=True)
class OrderOutcome:
    state: str
    oid: str | None = None
    filled_size: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    reason: str | None = None
    raw: dict | None = None


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    perp_state: dict
    spot_state: dict | None
    abstraction: str
    account_value: Decimal
    free_margin: Decimal
    collateral_balance: Decimal
    unrealized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class PositionConfig:
    leverage: int
    is_cross: bool


def deterministic_cloid(copy_job_id: str, attempt_kind: str) -> str:
    return '0x' + hashlib.blake2b(f'{copy_job_id}:{attempt_kind}'.encode(), digest_size=16).hexdigest()


def fill_event_id(fill: dict[str, Any]) -> str:
    return f"{fill.get('hash','')}:{fill.get('oid','')}:{fill.get('tid','')}"


def signed_fill_delta(fill: dict[str, Any]) -> Decimal:
    size = Decimal(str(fill.get('sz', '0')))
    side = str(fill.get('side') or fill.get('dir') or '').lower()
    if side in {'b', 'buy', 'open long', 'close short'} or 'long' in side and 'close long' not in side:
        return size
    return -size


def position_configs(state: dict) -> dict[str, PositionConfig]:
    """Extract per-market leverage/margin mode from clearinghouseState."""
    out: dict[str, PositionConfig] = {}
    for row in state.get('assetPositions', []):
        position = row.get('position', row)
        asset = str(position.get('coin') or '')
        if not asset:
            continue
        leverage = position.get('leverage') or {}
        try:
            value = max(int(Decimal(str(leverage.get('value', 1)))), 1)
        except Exception:
            value = 1
        out[asset] = PositionConfig(
            leverage=value,
            is_cross=str(leverage.get('type') or 'cross').lower() != 'isolated',
        )
    return out


def _decimal(value: Any) -> Decimal:
    if value in (None, ''):
        return Decimal(0)
    return Decimal(str(value))


def _perp_account_value(state: dict) -> Decimal:
    return _decimal(state.get('marginSummary', {}).get('accountValue'))


def _perp_free_margin(state: dict) -> Decimal:
    raw = state.get('withdrawable')
    if raw not in (None, ''):
        return max(_decimal(raw), Decimal(0))
    summary = state.get('marginSummary', {})
    return max(_decimal(summary.get('accountValue')) - _decimal(summary.get('totalMarginUsed')), Decimal(0))


def _spot_usdc(state: dict | None) -> tuple[Decimal, Decimal]:
    if not state:
        return Decimal(0), Decimal(0)
    for row in state.get('balances', []):
        if str(row.get('coin', '')).upper() == 'USDC' or row.get('token') == 0:
            return _decimal(row.get('total')), _decimal(row.get('hold'))
    return Decimal(0), Decimal(0)


def _unrealized_pnl(state: dict) -> Decimal:
    total = Decimal(0)
    for row in state.get('assetPositions', []):
        position = row.get('position', row)
        total += _decimal(position.get('unrealizedPnl'))
    return total


def _transient_read_error(exc: Exception) -> bool:
    """Whether an idempotent REST read is safe and useful to retry."""
    msg = str(exc).lower()
    return any(token in msg for token in (
        '502', '503', '504', 'bad gateway', 'service unavailable',
        'gateway timeout', 'connection reset', 'connection aborted',
        'temporarily unavailable', 'timed out', 'timeout',
    ))


def _explicit_rate_limit_reason(response: object) -> str | None:
    """Return a definite action-level throttle reason from a successful HTTP response."""
    if not isinstance(response, dict):
        return None

    try:
        statuses = response.get('response', {}).get('data', {}).get('statuses', [])
        for status in statuses:
            if not isinstance(status, dict) or 'error' not in status:
                continue
            reason = str(status.get('error') or '')
            if is_exchange_rate_limit_error(RuntimeError(reason)):
                return reason
    except Exception:
        pass

    if response.get('status') not in (None, 'ok'):
        for key in ('error', 'message', 'response'):
            reason = response.get(key)
            if reason is None:
                continue
            text = str(reason)
            if is_exchange_rate_limit_error(RuntimeError(text)):
                return text
    return None


class HyperliquidAdapter:
    def __init__(self, limiter: WeightedRateLimiter | None, network: Network | None = None):
        self.limiter = limiter
        self.network: Network = network or settings.follower_network
        self.api_url = settings.hyperliquid_url_for(self.network)
        self.ws_url = self.api_url.replace('https://', 'wss://') + '/ws'
        # Info normally performs spotMeta + meta HTTP requests in its constructor.
        # Those eager calls bypass our shared limiter/retry path and can crash the
        # worker before it starts when Hyperliquid briefly returns 502. Seed empty
        # metadata because this adapter uses Info only for direct read methods;
        # real perp metadata is fetched lazily by asset_spec through _read().
        self.info = Info(
            self.api_url,
            skip_ws=True,
            meta={'universe': []},
            spot_meta={'universe': [], 'tokens': []},
        )
        self._specs: dict[str, tuple[float, AssetSpec]] = {}
        self._perp_meta: Meta | None = None
        self._abstraction_cache: dict[str, tuple[float, str]] = {}
        self.address_limits = (
            AddressActionTracker(limiter._redis, self.network)
            if limiter is not None
            else None
        )

    async def _metric_incr(self, name: str) -> None:
        try:
            if self.limiter is not None:
                await self.limiter._redis.incr(f'hypercopy:metrics:{name}')
        except Exception:
            pass

    async def _call(self, func, *args):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            msg = str(exc).lower()
            if '429' in msg or 'rate limit' in msg or 'too many requests' in msg:
                await self._metric_incr('hl_429_count')
            if any(token in msg for token in ('502', '503', '504', 'bad gateway', 'service unavailable', 'gateway timeout')):
                await self._metric_incr('hl_5xx_count')
            raise

    async def _record_exchange_address_throttle(self, account_address: str) -> None:
        """Capture authoritative userRateLimit evidence after a definite rejection."""
        if self.address_limits is None:
            return
        try:
            snapshot = await self.user_rate_limit(account_address, priority=Priority.DIAGNOSTIC)
            await self.address_limits.record_exchange_snapshot(account_address, snapshot)
        except Exception:
            # Diagnostic evidence must never alter the already-known action result.
            pass

    async def _observe_explicit_address_throttle(self, account_address: str, reason: object) -> None:
        """Refresh authoritative quota evidence after a known rate-limit rejection."""
        if self.address_limits is None:
            return
        if not is_exchange_rate_limit_error(RuntimeError(str(reason))):
            return
        # The one-shot slot was already installed by _signed_call while the
        # signer advisory lock was still held. Keep only the slower diagnostic
        # request outside that critical section.
        await self._record_exchange_address_throttle(account_address)

    async def _signed_call(
        self,
        account_address: str,
        signer_address: str,
        func,
        *args,
        before_submit: Callable[[], Awaitable[None]] | None = None,
    ):
        """Run one signed SDK action under signer serialization and address cadence.

        A lightweight pre-wait happens outside the signer lock. The authoritative
        cadence reservation is then performed from the worker thread that is
        about to invoke the synchronous SDK function, eliminating thread-pool
        queue time between reservation and actual exchange invocation. An optional
        action-specific authorization callback runs after every asynchronous
        submission wait and immediately before the synchronous SDK call. Existing
        copy-trading actions do not install this callback, so their path is
        unchanged. The signer lock remains held through reservation, signing,
        network I/O and result classification.
        """

        if self.address_limits is not None:
            await self.address_limits.wait_for_existing_backoff(account_address)

        async def _mark_explicit_result(response: object) -> None:
            if self.address_limits is None:
                return
            if _explicit_rate_limit_reason(response) is not None:
                await self.address_limits.mark_throttled(account_address)

        loop = asyncio.get_running_loop()

        async def _prepare_actual_submission() -> None:
            if self.address_limits is not None:
                # Reserve/account for the actual action before the final optional
                # authorization. The callback is deliberately the last await so
                # no lock, rate-budget or cadence wait can make its decision stale.
                await self.address_limits.wait_if_backed_off(account_address)
                await self.address_limits.record_action_attempt(account_address)
            if before_submit is not None:
                await before_submit()

        def _invoke_after_cadence():
            if self.address_limits is not None or before_submit is not None:
                preparation = asyncio.run_coroutine_threadsafe(_prepare_actual_submission(), loop)
                preparation.result()
            return func(*args)

        async with signer_action_lock(signer_address):
            await self._acquire(WEIGHT_EXCHANGE_ACTION, Priority.ORDER, timeout=5)
            call_task = asyncio.create_task(self._call(_invoke_after_cadence))
            try:
                response = await asyncio.shield(call_task)
                await _mark_explicit_result(response)
                return response
            except asyncio.CancelledError:
                try:
                    response = await asyncio.shield(call_task)
                    await _mark_explicit_result(response)
                except Exception as exc:
                    if self.address_limits is not None and is_exchange_rate_limit_error(exc):
                        await self.address_limits.mark_throttled(account_address)
                raise
            except Exception as exc:
                if self.address_limits is not None and is_exchange_rate_limit_error(exc):
                    await self.address_limits.mark_throttled(account_address)
                raise

    async def _acquire(self, weight: int, priority: Priority, timeout: int | float) -> None:
        if self.limiter is None:
            return
        await self.limiter.acquire(weight, priority, timeout=timeout)

    async def _read(self, func, *args, weight: int, priority: Priority, timeout: int | float):
        """Retry only idempotent Hyperliquid reads, accounting for every try.

        Exchange actions never pass through this helper. Each retry acquires its
        own weighted budget before issuing another HTTP request, so resilience
        cannot silently exceed the shared IP quota.
        """
        attempts = settings.HL_SAFE_READ_RETRIES
        for attempt in range(attempts):
            await self._acquire(weight, priority, timeout=timeout)
            try:
                return await self._call(func, *args)
            except Exception as exc:
                if attempt + 1 >= attempts or not _transient_read_error(exc):
                    raise
                await self._metric_incr('hl_safe_read_retry_count')
                delay = settings.HL_SAFE_READ_BACKOFF_SECONDS * (2 ** attempt)
                if delay > 0:
                    await asyncio.sleep(delay)
        raise RuntimeError('unreachable Hyperliquid read retry state')

    async def user_state(self, address: str, *, priority: Priority = Priority.RECONCILE) -> dict:
        return await self._read(
            self.info.user_state, address,
            weight=WEIGHT_CHEAP_INFO, priority=priority, timeout=10,
        )

    async def spot_user_state(self, address: str, *, priority: Priority = Priority.RECONCILE) -> dict:
        return await self._read(
            self.info.spot_user_state, address,
            weight=WEIGHT_CHEAP_INFO, priority=priority, timeout=10,
        )

    async def user_abstraction(self, address: str, *, priority: Priority = Priority.RECONCILE) -> str:
        key = address.lower()
        cached = self._abstraction_cache.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        value = await self._read(
            self.info.post, '/info', {'type': 'userAbstraction', 'user': address},
            weight=WEIGHT_STANDARD_INFO, priority=priority, timeout=15,
        )
        abstraction = str(value or 'default')
        self._abstraction_cache[key] = (time.monotonic() + 300, abstraction)
        return abstraction

    async def user_rate_limit(self, address: str, *, priority: Priority = Priority.DIAGNOSTIC) -> dict:
        """Read Hyperliquid's authoritative per-user action quota status."""
        return await self._read(
            self.info.post,
            '/info',
            {'type': 'userRateLimit', 'user': address},
            weight=WEIGHT_STANDARD_INFO,
            priority=priority,
            timeout=15,
        )

    async def address_rate_limit_snapshot(self, address: str) -> dict[str, object]:
        if self.address_limits is None:
            return {
                'network': self.network,
                'address': address.lower(),
                'tracking': 'disabled_without_shared_limiter',
            }
        return (await self.address_limits.snapshot(address)).as_dict()

    async def account_snapshot(self, address: str, *, priority: Priority = Priority.RECONCILE) -> AccountSnapshot:
        perp = await self.user_state(address, priority=priority)
        abstraction = await self.user_abstraction(address, priority=priority)
        unrealized_pnl = _unrealized_pnl(perp)

        if abstraction == 'portfolioMargin':
            raise ValueError('Portfolio Margin accounts are not yet supported by HyperCopy')

        if abstraction == 'unifiedAccount':
            spot = await self.spot_user_state(address, priority=priority)
            usdc_total, usdc_hold = _spot_usdc(spot)
            account_value = max(usdc_total + unrealized_pnl, Decimal(0))
            margin_used = _decimal(perp.get('marginSummary', {}).get('totalMarginUsed'))
            free_margin = max(account_value - usdc_hold - margin_used, Decimal(0))
            return AccountSnapshot(
                perp, spot, abstraction, account_value, free_margin,
                usdc_total, unrealized_pnl,
            )

        account_value = _perp_account_value(perp)
        return AccountSnapshot(
            perp_state=perp,
            spot_state=None,
            abstraction=abstraction,
            account_value=account_value,
            free_margin=_perp_free_margin(perp),
            collateral_balance=account_value - unrealized_pnl,
            unrealized_pnl=unrealized_pnl,
        )

    async def mids(self) -> dict[str, str]:
        # allMids is shared market data used directly on the follower order hot
        # path. It is not master account state. Charging it to MASTER_STATE let
        # normal follower sizing consume the capacity reserved for verified
        # master equity/positions/leverage. Keep it in the ORDER lane instead;
        # reconciliation only calls it a handful of times per cycle.
        return await self._read(
            self.info.all_mids,
            weight=WEIGHT_CHEAP_INFO, priority=Priority.ORDER, timeout=10,
        )

    async def extra_agents(self, account: str) -> list[dict]:
        return await self._read(
            self.info.extra_agents, account,
            weight=WEIGHT_STANDARD_INFO, priority=Priority.METADATA, timeout=15,
        )

    async def verify_agent(
        self,
        account_address: str,
        private_key: str,
        expected_agent_address: str | None = None,
    ) -> AgentVerification:
        main = account_address.lower()
        local = Account.from_key(private_key)
        agent = local.address.lower()
        if expected_agent_address and agent != expected_agent_address.lower():
            raise ValueError('API Wallet address does not match the supplied private key')
        if agent == main:
            raise ValueError('Main wallet private keys are not accepted. Create a named Hyperliquid API wallet.')
        agents = await self.extra_agents(main)
        now_ms = int(time.time() * 1000)
        for item in agents:
            if str(item.get('address', '')).lower() != agent:
                continue
            name = str(item.get('name') or '')
            valid = item.get('validUntil')
            if settings.HL_AGENT_NAME and name and name != settings.HL_AGENT_NAME:
                continue
            if valid is not None and int(valid) <= now_ms:
                raise ValueError('Hyperliquid API wallet is expired')
            return AgentVerification(agent, name or settings.HL_AGENT_NAME, int(valid) if valid is not None else None)
        raise ValueError(f'Agent {agent} is not an active approved API wallet for the supplied account')

    async def asset_spec(self, asset: str) -> AssetSpec:
        cached = self._specs.get(asset)
        if cached and cached[0] > time.monotonic() and self._perp_meta is not None:
            return cached[1]
        meta = await self._read(
            self.info.meta,
            weight=WEIGHT_STANDARD_INFO, priority=Priority.METADATA, timeout=15,
        )
        self._perp_meta = meta
        expiry = time.monotonic() + settings.HL_MARKET_CACHE_TTL_SECONDS
        for row in meta.get('universe', []):
            spec = AssetSpec(
                name=row['name'],
                sz_decimals=int(row['szDecimals']),
                max_leverage=int(row.get('maxLeverage', 1)),
                only_isolated=bool(row.get('onlyIsolated', False)),
            )
            self._specs[spec.name] = (expiry, spec)
        if asset not in self._specs:
            raise ValueError(f'Unknown Hyperliquid perpetual asset: {asset}')
        return self._specs[asset][1]

    def _exchange(self, local, account_address: str) -> Exchange:
        """Build Exchange without SDK constructor network calls.

        asset_spec() must run first so the real perp metadata is available for
        name_to_asset signing. Supplying both meta objects prevents Exchange's
        nested Info constructor from issuing its own eager spotMeta/meta reads.
        """
        if self._perp_meta is None:
            raise RuntimeError('Perp metadata must be loaded before Exchange construction')
        return Exchange(
            local,
            self.api_url,
            meta=self._perp_meta,
            account_address=account_address,
            spot_meta={'universe': [], 'tokens': []},
        )

    async def update_leverage(
        self,
        *,
        account_address: str,
        private_key: str,
        asset: str,
        leverage: int,
        is_cross: bool,
        before_submit: Callable[[], Awaitable[tuple[int, bool] | None]] | None = None,
    ) -> dict:
        """Set the follower market's leverage/margin mode using its API wallet."""
        spec = await self.asset_spec(asset)
        effective = {
            'leverage': max(1, min(int(leverage), spec.max_leverage)),
            'is_cross': False if spec.only_isolated else bool(is_cross),
        }
        local = Account.from_key(private_key)
        exchange = self._exchange(local, account_address)

        async def _apply_before_submit() -> None:
            if before_submit is None:
                return
            override = await before_submit()
            if override is None:
                return
            override_leverage, override_cross = override
            effective['leverage'] = max(1, min(int(override_leverage), spec.max_leverage))
            effective['is_cross'] = False if spec.only_isolated else bool(override_cross)

        def _submit_leverage():
            # Calculate expiration only after the signer lock is held; otherwise
            # lock contention could age expiresAfter before the action is sent.
            exchange.set_expires_after(int(time.time() * 1000) + settings.HL_ORDER_EXPIRES_AFTER_MS)
            return exchange.update_leverage(
                int(effective['leverage']),
                asset,
                bool(effective['is_cross']),
            )

        # Exchange actions are intentionally one-shot. Ambiguous results are
        # resolved later by Cloid rather than blindly resubmitted. Only callers
        # that explicitly supply before_submit get the extra action-boundary
        # authorization; ordinary copy-trading orders keep their existing path.
        response = await self._signed_call(
            account_address,
            local.address,
            _submit_leverage,
            before_submit=_apply_before_submit if before_submit is not None else None,
        )
        if not isinstance(response, dict) or response.get('status') not in (None, 'ok'):
            await self._observe_explicit_address_throttle(account_address, response)
            raise RuntimeError(f'Hyperliquid leverage update failed: {response}')
        return response

    async def query_order_by_cloid(self, account: str, cloid: str) -> dict:
        return await self._read(
            self.info.query_order_by_cloid, account, Cloid.from_str(cloid),
            weight=WEIGHT_CHEAP_INFO, priority=Priority.ORDER, timeout=10,
        )

    async def user_fills_by_time(self, account: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        return await self._read(
            self.info.user_fills_by_time, account, start_ms, end_ms,
            weight=WEIGHT_USER_FILLS_MAX, priority=Priority.RECONCILE, timeout=30,
        )

    async def place_ioc(
        self, *, account_address: str, private_key: str, asset: str, is_buy: bool,
        size: Decimal, mark_price: Decimal, slippage_bps: int, reduce_only: bool, cloid: str,
    ) -> OrderOutcome:
        spec = await self.asset_spec(asset)
        slip = Decimal(slippage_bps) / Decimal(10_000)
        aggressive = mark_price * (Decimal(1) + slip if is_buy else Decimal(1) - slip)
        px = round_price(aggressive, spec.sz_decimals)
        local = Account.from_key(private_key)
        exchange = self._exchange(local, account_address)

        def _submit():
            exchange.set_expires_after(int(time.time() * 1000) + settings.HL_ORDER_EXPIRES_AFTER_MS)
            return exchange.order(
                asset, is_buy, float(size), float(px), {'limit': {'tif': 'Ioc'}},
                reduce_only=reduce_only, cloid=Cloid.from_str(cloid),
            )

        response = await self._signed_call(account_address, local.address, _submit)
        outcome = parse_order_response(response)
        if outcome.state == 'REJECTED':
            await self._observe_explicit_address_throttle(account_address, outcome.reason)
        return outcome

    async def _heartbeat(self, ws, stop_event: asyncio.Event) -> None:
        """Send Hyperliquid's documented JSON heartbeat independent of fills."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.HL_WS_HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                await ws.send(json.dumps({'method': 'ping'}))
                await self._metric_incr('ws_heartbeat_count')

    async def master_fills(self, address: str, stop_event: asyncio.Event) -> AsyncIterator[dict]:
        try:
            async with websockets.connect(
                self.ws_url,
                # Hyperliquid documents an application-level JSON heartbeat.
                # Disable protocol ping frames and run that heartbeat ourselves.
                ping_interval=None,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({
                    'method': 'subscribe',
                    'subscription': {'type': 'userFills', 'user': address},
                }))
                heartbeat = asyncio.create_task(self._heartbeat(ws, stop_event))
                try:
                    while not stop_event.is_set():
                        try:
                            # Periodically return control to observe SIGTERM while
                            # the heartbeat task independently keeps the socket alive.
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                        except TimeoutError:
                            continue

                        message = json.loads(raw)
                        channel = message.get('channel')
                        if channel in {'pong', 'subscriptionResponse'}:
                            continue
                        if channel != 'userFills':
                            continue

                        data = message.get('data', {})
                        fills = data.get('fills', []) if isinstance(data, dict) else []
                        for fill in fills:
                            yield fill
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosedOK as exc:
            await self._metric_incr('ws_session_rotation_count')
            log.info('Master websocket session closed normally; replay required', extra={'state': str(exc), 'network': self.network})
            return
        except Exception:
            await self._metric_incr('ws_reconnect_count')
            log.warning('Master websocket disconnected; watcher will replay before reconnect', extra={'network': self.network}, exc_info=True)
            raise


def parse_order_response(response: dict) -> OrderOutcome:
    try:
        statuses = response['response']['data']['statuses']
        first = statuses[0]
    except Exception:
        return OrderOutcome('UNKNOWN', reason='Unexpected exchange response shape', raw=response)
    if 'filled' in first:
        f = first['filled']
        return OrderOutcome('FILLED', str(f.get('oid')) if f.get('oid') is not None else None,
                            Decimal(str(f.get('totalSz', '0'))), Decimal(str(f.get('avgPx'))) if f.get('avgPx') else None, raw=response)
    if 'resting' in first:
        return OrderOutcome('UNKNOWN', str(first['resting'].get('oid')), reason='IOC returned resting status', raw=response)
    if 'error' in first:
        return OrderOutcome('REJECTED', reason=str(first['error']), raw=response)
    return OrderOutcome('UNKNOWN', reason='Unclassified exchange status', raw=response)
