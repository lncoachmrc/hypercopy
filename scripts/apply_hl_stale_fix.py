from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text()
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f'{path}: expected exactly one match, found {count}')
    file_path.write_text(text.replace(old, new, 1))


replace_once(
    'backend/app/adapters/hyperliquid.py',
    """        self.info = Info(\n            self.api_url,\n            skip_ws=True,\n            meta={'universe': []},\n            spot_meta={'universe': [], 'tokens': []},\n        )\n        self._specs: dict[str, tuple[float, AssetSpec]] = {}\n        self._perp_meta: Meta | None = None\n        self._abstraction_cache: dict[str, tuple[float, str]] = {}\n""",
    """        self.info = Info(\n            self.api_url,\n            skip_ws=True,\n            meta={'universe': []},\n            spot_meta={'universe': [], 'tokens': []},\n            timeout=10.0,\n        )\n        self._specs: dict[str, tuple[float, AssetSpec]] = {}\n        self._perp_meta: Meta | None = None\n        self._abstraction_cache: dict[str, tuple[float, str]] = {}\n        # Reconciliation/diagnostic reads may be deferred briefly after an\n        # exchange-level 429. ORDER, MASTER_STATE and METADATA remain available\n        # so the circuit breaker cannot starve the execution hot path.\n        self._read_cooldown_seconds = 30.0\n        self._read_cooldown_until = 0.0\n""",
)

replace_once(
    'backend/app/adapters/hyperliquid.py',
    """    async def _read(self, func, *args, weight: int, priority: Priority, timeout: int | float):\n        \"\"\"Retry only idempotent Hyperliquid reads, accounting for every try.\n\n        Exchange actions never pass through this helper. Each retry acquires its\n        own weighted budget before issuing another HTTP request, so resilience\n        cannot silently exceed the shared IP quota.\n        \"\"\"\n        attempts = settings.HL_SAFE_READ_RETRIES\n        for attempt in range(attempts):\n            await self._acquire(weight, priority, timeout=timeout)\n            try:\n                return await self._call(func, *args)\n            except Exception as exc:\n                if attempt + 1 >= attempts or not _transient_read_error(exc):\n                    raise\n                await self._metric_incr('hl_safe_read_retry_count')\n                delay = settings.HL_SAFE_READ_BACKOFF_SECONDS * (2 ** attempt)\n                if delay > 0:\n                    await asyncio.sleep(delay)\n        raise RuntimeError('unreachable Hyperliquid read retry state')\n""",
    """    async def _read(self, func, *args, weight: int, priority: Priority, timeout: int | float):\n        \"\"\"Retry only bounded, idempotent Hyperliquid reads.\n\n        The rate-limiter wait and the synchronous SDK call have independent\n        deadlines. Exchange actions never pass through this helper. A 429 on\n        reconciliation/diagnostic traffic opens a short local circuit breaker;\n        order-critical lanes stay available.\n        \"\"\"\n        attempts = max(int(settings.HL_SAFE_READ_RETRIES), 1)\n        cooldown_priorities = {Priority.RECONCILE, Priority.DIAGNOSTIC}\n        for attempt in range(attempts):\n            if priority in cooldown_priorities:\n                remaining = self._read_cooldown_until - time.monotonic()\n                if remaining > 0:\n                    await self._metric_incr('hl_read_cooldown_skip_count')\n                    raise RuntimeError(\n                        f'Hyperliquid read cooldown active for {remaining:.1f}s after rate limit'\n                    )\n\n            await self._acquire(weight, priority, timeout=timeout)\n            try:\n                return await asyncio.wait_for(\n                    self._call(func, *args),\n                    timeout=float(timeout),\n                )\n            except TimeoutError as exc:\n                await self._metric_incr('hl_read_timeout_count')\n                log.warning(\n                    'Hyperliquid read timed out',\n                    extra={'event_code': 'HL_READ_TIMEOUT', 'network': self.network},\n                )\n                if attempt + 1 >= attempts:\n                    raise TimeoutError(\n                        f'Hyperliquid read timed out after {float(timeout):g}s'\n                    ) from exc\n            except Exception as exc:\n                if is_exchange_rate_limit_error(exc) and priority in cooldown_priorities:\n                    self._read_cooldown_until = max(\n                        self._read_cooldown_until,\n                        time.monotonic() + self._read_cooldown_seconds,\n                    )\n                    await self._metric_incr('hl_read_cooldown_activated_count')\n                    log.warning(\n                        'Hyperliquid low-priority read cooldown activated',\n                        extra={\n                            'event_code': 'HL_READ_RATE_LIMIT_COOLDOWN',\n                            'network': self.network,\n                        },\n                    )\n                if attempt + 1 >= attempts or not _transient_read_error(exc):\n                    raise\n\n            await self._metric_incr('hl_safe_read_retry_count')\n            delay = settings.HL_SAFE_READ_BACKOFF_SECONDS * (2 ** attempt)\n            if delay > 0:\n                await asyncio.sleep(delay)\n        raise RuntimeError('unreachable Hyperliquid read retry state')\n""",
)

replace_once(
    'backend/app/adapters/hyperliquid.py',
    """    async def mids(self) -> dict[str, str]:\n        # allMids is shared market data used directly on the follower order hot\n        # path. It is not master account state. Charging it to MASTER_STATE let\n        # normal follower sizing consume the capacity reserved for verified\n        # master equity/positions/leverage. Keep it in the ORDER lane instead;\n        # reconciliation only calls it a handful of times per cycle.\n        return await self._read(\n            self.info.all_mids,\n            weight=WEIGHT_CHEAP_INFO, priority=Priority.ORDER, timeout=10,\n        )\n""",
    """    async def mids(self, *, priority: Priority = Priority.ORDER) -> dict[str, str]:\n        # allMids is shared market data used directly on the follower order hot\n        # path, where ORDER remains the default. Reconciliation passes its own\n        # lane explicitly so maintenance cannot consume order-path capacity.\n        return await self._read(\n            self.info.all_mids,\n            weight=WEIGHT_CHEAP_INFO, priority=priority, timeout=10,\n        )\n""",
)

replace_once(
    'backend/app/workers/execution_worker.py',
    "from app.adapters.hyperliquid import HyperliquidAdapter, position_configs\n",
    "from app.adapters.address_ratelimit import is_exchange_rate_limit_error\nfrom app.adapters.hyperliquid import HyperliquidAdapter, position_configs\n",
)

replace_once(
    'backend/app/workers/execution_worker.py',
    """    async def maintenance(self):\n        while not stop.is_set():\n""",
    """    async def _run_reconcile_with_deadline(self, timeout: float | None = None) -> bool:\n        if timeout is None:\n            interval = float(settings.RECONCILE_INTERVAL_SECONDS)\n            timeout = max(5.0, min(45.0, interval * 0.75))\n        try:\n            await asyncio.wait_for(self.run_reconcile_if_leader(), timeout=float(timeout))\n            return True\n        except TimeoutError:\n            log.error(\n                f'Reconciliation cycle timed out after {float(timeout):g}s',\n                extra={'event_code': 'RECONCILIATION_TIMEOUT'},\n            )\n            return False\n\n    async def maintenance(self):\n        while not stop.is_set():\n""",
)

replace_once(
    'backend/app/workers/execution_worker.py',
    "            await self.run_reconcile_if_leader()\n",
    "            await self._run_reconcile_with_deadline()\n",
)

replace_once(
    'backend/app/workers/execution_worker.py',
    "            mids=await follower_hl.mids()\n",
    "            mids=await follower_hl.mids(priority=Priority.RECONCILE)\n",
)

replace_once(
    'backend/app/workers/execution_worker.py',
    """        return refreshed\n\n    async def run_reconcile_if_leader(self):\n""",
    """        return refreshed\n\n    async def _fallback_observability_after_reconcile_failure(\n        self,\n        db,\n        network: Network,\n        exc: Exception,\n    ) -> int:\n        error_info = (type(exc), exc, exc.__traceback__)\n        if is_exchange_rate_limit_error(exc):\n            log.warning(\n                'Follower observability refresh deferred after Hyperliquid rate limit',\n                extra={\n                    'event_code': 'FOLLOWER_OBSERVABILITY_RATE_LIMIT_DEFERRED',\n                    'master_network': settings.master_network,\n                    'follower_network': network,\n                },\n                exc_info=error_info,\n            )\n            return 0\n\n        log.warning(\n            'Full reconciliation failed; refreshing follower observability only',\n            extra={\n                'master_network': settings.master_network,\n                'follower_network': network,\n            },\n            exc_info=error_info,\n        )\n        refreshed = await self._refresh_follower_observability(db, network)\n        log.info(\n            'Follower observability refresh completed',\n            extra={'follower_network': network, 'users_refreshed': refreshed},\n        )\n        return refreshed\n\n    async def run_reconcile_if_leader(self):\n""",
)

replace_once(
    'backend/app/workers/execution_worker.py',
    """                        except Exception:\n                            await db.rollback()\n                            log.warning(\n                                'Full reconciliation failed; refreshing follower observability only',\n                                extra={'master_network':settings.master_network,'follower_network':network},\n                                exc_info=True,\n                            )\n                            refreshed=await self._refresh_follower_observability(db,network)\n                            log.info(\n                                'Follower observability refresh completed',\n                                extra={'follower_network':network,'users_refreshed':refreshed},\n                            )\n""",
    """                        except Exception as exc:\n                            await db.rollback()\n                            await self._fallback_observability_after_reconcile_failure(\n                                db, network, exc\n                            )\n""",
)

replace_once(
    'backend/app/services/reconcile.py',
    "source_mids = observed_master_mids(await source_hl.mids(), source_snapshot_started_order)\n",
    "source_mids = observed_master_mids(\n        await source_hl.mids(priority=Priority.RECONCILE),\n        source_snapshot_started_order,\n    )\n",
)

replace_once(
    'backend/app/services/reconcile.py',
    "follower_mids = source_mids if source_hl.network == hl.network else await hl.mids()\n",
    "follower_mids = (\n        source_mids\n        if source_hl.network == hl.network\n        else await hl.mids(priority=Priority.RECONCILE)\n    )\n",
)

print('Hyperliquid stale-data remediation patches applied successfully.')
