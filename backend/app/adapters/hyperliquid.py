from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, AsyncIterator

import websockets
from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.types import Cloid

from app.core.config import settings
from app.core.logging import get_logger
from app.engine.sizing import AssetSpec, round_price
from app.adapters.ratelimit import Priority, WEIGHT_CHEAP_INFO, WEIGHT_EXCHANGE_ACTION, WEIGHT_STANDARD_INFO, WEIGHT_USER_FILLS_MAX, WeightedRateLimiter

log = get_logger(__name__)


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


class HyperliquidAdapter:
    def __init__(self, limiter: WeightedRateLimiter):
        self.limiter = limiter
        self.info = Info(settings.hyperliquid_api_url, skip_ws=True)
        self._specs: dict[str, tuple[float, AssetSpec]] = {}

    async def _metric_incr(self, name: str) -> None:
        try:
            await self.limiter._redis.incr(f'hypercopy:metrics:{name}')  # shared Redis metric only
        except Exception:
            pass

    async def _call(self, func, *args):
        try:
            return await asyncio.to_thread(func, *args)
        except Exception as exc:
            msg = str(exc).lower()
            if '429' in msg or 'rate limit' in msg or 'too many requests' in msg:
                await self._metric_incr('hl_429_count')
            raise

    async def user_state(self, address: str, *, priority: Priority = Priority.RECONCILE) -> dict:
        await self.limiter.acquire(WEIGHT_CHEAP_INFO, priority, timeout=10)
        return await self._call(self.info.user_state, address)

    async def mids(self) -> dict[str, str]:
        await self.limiter.acquire(WEIGHT_CHEAP_INFO, Priority.MASTER_STATE, timeout=10)
        return await self._call(self.info.all_mids)

    async def extra_agents(self, account: str) -> list[dict]:
        await self.limiter.acquire(WEIGHT_STANDARD_INFO, Priority.METADATA, timeout=15)
        return await self._call(self.info.extra_agents, account)

    async def verify_agent(self, account_address: str, private_key: str) -> AgentVerification:
        main = account_address.lower()
        local = Account.from_key(private_key)
        agent = local.address.lower()
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
        if cached and cached[0] > time.monotonic():
            return cached[1]
        await self.limiter.acquire(WEIGHT_STANDARD_INFO, Priority.METADATA, timeout=15)
        meta = await self._call(self.info.meta)
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

    async def query_order_by_cloid(self, account: str, cloid: str) -> dict:
        await self.limiter.acquire(WEIGHT_CHEAP_INFO, Priority.ORDER, timeout=10)
        return await self._call(self.info.query_order_by_cloid, account, Cloid.from_str(cloid))

    async def user_fills_by_time(self, account: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        await self.limiter.acquire(WEIGHT_USER_FILLS_MAX, Priority.RECONCILE, timeout=30)
        return await self._call(self.info.user_fills_by_time, account, start_ms, end_ms)

    async def place_ioc(
        self, *, account_address: str, private_key: str, asset: str, is_buy: bool,
        size: Decimal, mark_price: Decimal, slippage_bps: int, reduce_only: bool, cloid: str,
    ) -> OrderOutcome:
        await self.limiter.acquire(WEIGHT_EXCHANGE_ACTION, Priority.ORDER, timeout=5)
        spec = await self.asset_spec(asset)
        slip = Decimal(slippage_bps) / Decimal(10_000)
        aggressive = mark_price * (Decimal(1) + slip if is_buy else Decimal(1) - slip)
        px = round_price(aggressive, spec.sz_decimals)
        local = Account.from_key(private_key)
        exchange = Exchange(local, settings.hyperliquid_api_url, account_address=account_address)
        exchange.set_expires_after(int(time.time() * 1000) + settings.HL_ORDER_EXPIRES_AFTER_MS)

        def _submit():
            return exchange.order(
                asset, is_buy, float(size), float(px), {'limit': {'tif': 'Ioc'}},
                reduce_only=reduce_only, cloid=Cloid.from_str(cloid),
            )

        response = await self._call(_submit)
        return parse_order_response(response)

    async def master_fills(self, address: str, stop_event: asyncio.Event) -> AsyncIterator[dict]:
        """Consume one WebSocket session with Hyperliquid application heartbeats.

        Hyperliquid closes a websocket when it has not sent a message for 60s.
        Quiet user-specific feeds therefore require the documented JSON
        ``{"method":"ping"}`` heartbeat. Protocol-level websocket pings alone do
        not satisfy that application-level requirement. On a real disconnect we
        return control to the watcher so it replays durable fills before the next
        realtime session.
        """
        try:
            async with websockets.connect(
                settings.hyperliquid_ws_url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                await ws.send(json.dumps({
                    'method': 'subscribe',
                    'subscription': {'type': 'userFills', 'user': address},
                }))
                while not stop_event.is_set():
                    try:
                        # Heartbeat before Hyperliquid's documented 60s idle
                        # threshold. recv() cancellation is safe in websockets;
                        # no application message is lost by this timeout.
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except TimeoutError:
                        await ws.send(json.dumps({'method': 'ping'}))
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
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosedOK as exc:
            # Hyperliquid testnet may rotate a healthy session with close code
            # 1000 and reason "Expired". This is a normal lifecycle event, not
            # a watcher failure. The caller will replay from the durable
            # PostgreSQL checkpoint before opening the next realtime session.
            await self._metric_incr('ws_session_rotation_count')
            log.info('Master websocket session closed normally; replay required', extra={'state': str(exc)})
            return
        except Exception:
            await self._metric_incr('ws_reconnect_count')
            log.warning('Master websocket disconnected; watcher will replay before reconnect', exc_info=True)
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
        # IOC should not rest; if it does, treat it as unknown and reconcile.
        return OrderOutcome('UNKNOWN', str(first['resting'].get('oid')), reason='IOC returned resting status', raw=response)
    if 'error' in first:
        return OrderOutcome('REJECTED', reason=str(first['error']), raw=response)
    return OrderOutcome('UNKNOWN', reason='Unclassified exchange status', raw=response)
