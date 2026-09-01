from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_WRITER_KEY_PREFIX = 'hypercopy:writers:mainnet'

# Bootstrap must be race-safe under concurrent first-jobs from independent
# deployments: only one identity may ever "win" registration for a wallet that
# has no registered writer yet. SETNX on a sentinel plus SADD of the winning
# identity keeps the whole bootstrap atomic and idempotent under retries.
_BOOTSTRAP_SCRIPT = """
-- WRITER_IDENTITY_BOOTSTRAP
local set_key = KEYS[1]
local lock_key = KEYS[2]
local identity = ARGV[1]
if redis.call('EXISTS', set_key) == 1 then
  return redis.call('SISMEMBER', set_key, identity)
end
local acquired = redis.call('SETNX', lock_key, identity)
if acquired == 1 then
  redis.call('SADD', set_key, identity)
  return 1
end
local winner = redis.call('GET', lock_key)
if winner == identity then
  redis.call('SADD', set_key, identity)
  return 1
end
return 0
"""


def _writer_set_key(wallet: str) -> str:
    return f'{_WRITER_KEY_PREFIX}:{wallet.lower().strip()}'


def _writer_bootstrap_lock_key(wallet: str) -> str:
    return f'{_WRITER_KEY_PREFIX}:{wallet.lower().strip()}:bootstrap-lock'


class WriterIdentityRegistry:
    """Redis-backed single-writer registry for MAINNET strategy orders.

    Exactly one (or more, if manually reassigned) deployment identity is ever
    authorized to submit strategy (EVENT/RECONCILE) orders for a given MAINNET
    follower wallet. The registry is deliberately fail-closed: any error talking
    to Redis, or any missing/mismatched identity, must be treated by the caller
    as "not authorized" rather than "authorized".
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def register_identity(self, wallet: str) -> bool:
        """Attempt to register the local deployment identity for ``wallet``.

        Returns True only if the currently configured EXECUTION_WORKER_IDENTITY
        is (or becomes) an authorized writer for this wallet. Concurrent first
        registrations from independent deployments race safely: exactly one
        identity wins the bootstrap and all others observe False.
        """

        identity = settings.EXECUTION_WORKER_IDENTITY.strip()
        if not identity:
            return False
        try:
            result = await self._redis.eval(
                _BOOTSTRAP_SCRIPT,
                2,
                _writer_set_key(wallet),
                _writer_bootstrap_lock_key(wallet),
                identity,
            )
            return bool(int(result))
        except Exception:
            log.warning(
                'Writer identity bootstrap failed; treating as unauthorized',
                extra={'wallet': wallet},
                exc_info=True,
            )
            return False

    async def verify_writer_authority(self, wallet: str) -> bool:
        """Fail-closed check that the local identity currently holds authority."""

        identity = settings.EXECUTION_WORKER_IDENTITY.strip()
        if not identity:
            return False
        try:
            is_member = await self._redis.sismember(_writer_set_key(wallet), identity)
            if bool(is_member):
                return True
            # Not yet registered: attempt bootstrap so the first strategy job
            # for a wallet does not require a separate warm-up step.
            return await self.register_identity(wallet)
        except Exception:
            log.warning(
                'Writer authority verification failed; failing closed',
                extra={'wallet': wallet},
                exc_info=True,
            )
            return False

    async def revoke_identity(self, wallet: str, identity: str) -> bool:
        """Administrative removal of a previously authorized identity.

        Reserved for manual reassignment (e.g. decommissioning a deployment).
        Not invoked by the automated job-processing path.
        """

        try:
            removed = await self._redis.srem(_writer_set_key(wallet), identity)
            return bool(int(removed))
        except Exception:
            log.warning(
                'Writer identity revocation failed',
                extra={'wallet': wallet, 'identity': identity},
                exc_info=True,
            )
            return False
