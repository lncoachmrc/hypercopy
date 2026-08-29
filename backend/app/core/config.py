from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


Network = Literal['testnet', 'mainnet']


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', case_sensitive=True, extra='ignore')

    APP_ENV: Literal['development', 'staging', 'production'] = 'development'
    APP_VERSION: str = 'dev'
    LOG_LEVEL: str = 'INFO'
    PUBLIC_APP_URL: str = 'http://localhost:5173'
    API_BASE_URL: str = 'http://localhost:8000'

    DATABASE_URL: str = 'postgresql+asyncpg://hypercopy:hypercopy@postgres:5432/hypercopy'
    REDIS_URL: str = 'redis://redis:6379/0'

    # Legacy single-network settings are follower-side only. The configured
    # strategy master is an architectural MAINNET source and cannot be moved by
    # user settings or a stale deployment-level network override. TRAXION's
    # operational fallback is MAINNET; local/test environments can explicitly
    # override it to TESTNET (the repository .env.example does so).
    HYPERLIQUID_NETWORK: Network = 'mainnet'
    # Deprecated compatibility input. master_network intentionally ignores it.
    HYPERLIQUID_MASTER_NETWORK: Network | None = None
    HYPERLIQUID_FOLLOWER_NETWORK: Network | None = None
    HYPERLIQUID_MASTER_ADDRESS: str = ''
    HL_RATE_BUDGET_PER_MIN: int = 1200
    HL_ORDER_EXPIRES_AFTER_MS: int = 15_000
    HL_AGENT_NAME: str = 'hypercopy'
    HL_MARKET_CACHE_TTL_SECONDS: int = 300
    HL_REPLAY_MAX_FILLS: int = 10_000
    # Reuse one verified master account snapshot across bursts of fills instead
    # of spending a clearinghouseState request on every fill. A very short
    # last-known-good window bridges transient 429/5xx responses without
    # allowing old leverage data to live indefinitely.
    HL_MASTER_SNAPSHOT_TTL_SECONDS: float = 2.0
    HL_MASTER_SNAPSHOT_STALE_SECONDS: float = 15.0
    # Operational SLO for recovering authoritative master leverage after a
    # realtime fill could not carry it. The safety behavior remains fail-closed;
    # this value drives observability and alerting, never a guessed leverage.
    MASTER_LEVERAGE_RECOVERY_SLO_SECONDS: float = 60.0
    # Hyperliquid closes websocket connections that receive no server message
    # for 60s. Send the documented application heartbeat independently of fill
    # traffic so quiet subscriptions remain alive.
    HL_WS_HEARTBEAT_SECONDS: float = 30.0
    # Safe retries apply only to idempotent/read-only Hyperliquid calls. Exchange
    # actions are deliberately never automatically retried.
    HL_SAFE_READ_RETRIES: int = 3
    HL_SAFE_READ_BACKOFF_SECONDS: float = 0.5

    # Global mainnet execution gate. Per-user network selection never bypasses
    # this flag or the independent PostgreSQL live_trading flag.
    ENABLE_LIVE_TRADING: bool = False
    DEFAULT_SHADOW_MODE: bool = True

    SESSION_SECRET: str = 'development-only-change-me'
    # Short-lived access JWT. Browser renewal happens through the independent
    # rotating HttpOnly refresh credential below, so wallet signatures are not
    # requested every hour.
    SESSION_TTL_SECONDS: int = 3600
    SESSION_REFRESH_TTL_SECONDS: int = 86_400
    # The previous refresh credential remains an idempotent delivery handle for
    # this short interval. It can only reproduce its already-created successor;
    # it cannot extend the absolute 24-hour session window.
    SESSION_REFRESH_GRACE_SECONDS: int = 60
    SESSION_COOKIE_NAME: str = 'hc_session'
    SESSION_REFRESH_COOKIE_NAME: str = 'hc_refresh'
    CSRF_COOKIE_NAME: str = 'hc_csrf'
    SIWE_DOMAIN: str = 'localhost'
    SIWE_URI: str = 'http://localhost:5173'
    AUTH_NONCE_TTL_SECONDS: int = 300

    # Local: AES KEK from ENCRYPTION_KEY_B64. Production: AWS KMS is the
    # concrete external KMS implementation shipped with this repository.
    KEK_PROVIDER: Literal['env', 'aws_kms'] = 'env'
    ENCRYPTION_KEY_B64: str = ''
    ENCRYPTION_KEY_REFERENCE: str = ''
    AWS_REGION: str = ''

    STRIPE_SECRET_KEY: str = ''
    STRIPE_WEBHOOK_SECRET: str = ''

    # New portfolio-based commercial catalog.
    STRIPE_PRICE_STARTER_MONTHLY: str = ''
    STRIPE_PRICE_STARTER_YEARLY: str = ''
    STRIPE_PRICE_PLUS_MONTHLY: str = ''
    STRIPE_PRICE_PLUS_YEARLY: str = ''
    STRIPE_PRICE_PRO_MONTHLY: str = ''
    STRIPE_PRICE_PRO_YEARLY: str = ''

    # Legacy monthly price IDs remain supported as safe fallbacks while Stripe
    # products are migrated: BASIC -> Starter, PRO -> Plus, ENTERPRISE -> Pro.
    STRIPE_PRICE_BASIC: str = ''
    STRIPE_PRICE_PRO: str = ''
    STRIPE_PRICE_ENTERPRISE: str = ''
    TRIAL_DAYS: int = 14

    ADMIN_ADDRESSES: str = ''
    SUPERADMIN_ADDRESSES: str = ''
    RECONCILE_INTERVAL_SECONDS: int = 60
    LEDGER_STALE_SECONDS: int = 120
    WATCHER_LEASE_TTL_SECONDS: int = 15
    WATCHER_LEASE_RENEW_SECONDS: int = 5
    JOB_LEASE_SECONDS: int = 120
    MAX_JOB_RETRIES: int = 5
    # EVENT/RECONCILE jobs are point-in-time strategy intents. After this age
    # they must be rebuilt from current exchange truth instead of executed late.
    # CLOSE_ALL and administrative jobs are deliberately exempt.
    STRATEGY_JOB_MAX_AGE_SECONDS: int = 600
    STREAM_NAME: str = 'hypercopy:copy_jobs'
    STREAM_GROUP: str = 'execution-workers'
    REALTIME_CHANNEL_PREFIX: str = 'hypercopy:events'
    API_RATE_LIMIT_PER_MIN: int = 300
    METRICS_TOKEN: str = ''
    SENTRY_DSN: str = ''

    @field_validator('DATABASE_URL')
    @classmethod
    def async_database_url(cls, value: str) -> str:
        if value.startswith('postgres://'):
            value = 'postgresql://' + value[len('postgres://'):]
        if value.startswith('postgresql://'):
            value = 'postgresql+asyncpg://' + value[len('postgresql://'):]
        return value

    @field_validator('HYPERLIQUID_MASTER_ADDRESS')
    @classmethod
    def normalize_address(cls, value: str) -> str:
        return value.lower().strip()

    @model_validator(mode='after')
    def production_safety(self) -> 'Settings':
        if self.APP_ENV == 'production':
            if self.SESSION_SECRET == 'development-only-change-me' or len(self.SESSION_SECRET) < 32:
                raise ValueError('SESSION_SECRET must be a strong production secret')
            if self.ENABLE_LIVE_TRADING and self.KEK_PROVIDER == 'env':
                raise ValueError('Mainnet live execution requires an external KMS provider')
        if self.SESSION_TTL_SECONDS <= 0:
            raise ValueError('SESSION_TTL_SECONDS must be positive')
        if self.SESSION_REFRESH_TTL_SECONDS <= self.SESSION_TTL_SECONDS:
            raise ValueError('SESSION_REFRESH_TTL_SECONDS must be greater than SESSION_TTL_SECONDS')
        if not 1 <= self.SESSION_REFRESH_GRACE_SECONDS < self.SESSION_TTL_SECONDS:
            raise ValueError('SESSION_REFRESH_GRACE_SECONDS must be between 1 and SESSION_TTL_SECONDS')
        if self.WATCHER_LEASE_RENEW_SECONDS >= self.WATCHER_LEASE_TTL_SECONDS:
            raise ValueError('watcher lease renew interval must be lower than TTL')
        if self.HL_MASTER_SNAPSHOT_TTL_SECONDS <= 0:
            raise ValueError('HL_MASTER_SNAPSHOT_TTL_SECONDS must be positive')
        if self.HL_MASTER_SNAPSHOT_STALE_SECONDS < self.HL_MASTER_SNAPSHOT_TTL_SECONDS:
            raise ValueError('HL_MASTER_SNAPSHOT_STALE_SECONDS must be >= snapshot TTL')
        if self.MASTER_LEVERAGE_RECOVERY_SLO_SECONDS < self.HL_MASTER_SNAPSHOT_STALE_SECONDS:
            raise ValueError('MASTER_LEVERAGE_RECOVERY_SLO_SECONDS must be >= snapshot stale window')
        if not 0 < self.HL_WS_HEARTBEAT_SECONDS < 60:
            raise ValueError('HL_WS_HEARTBEAT_SECONDS must be between 1 and 59 seconds')
        if not 1 <= self.HL_SAFE_READ_RETRIES <= 5:
            raise ValueError('HL_SAFE_READ_RETRIES must be between 1 and 5')
        if self.HL_SAFE_READ_BACKOFF_SECONDS < 0:
            raise ValueError('HL_SAFE_READ_BACKOFF_SECONDS cannot be negative')
        if self.STRATEGY_JOB_MAX_AGE_SECONDS <= 0:
            raise ValueError('STRATEGY_JOB_MAX_AGE_SECONDS must be positive')
        return self

    @property
    def admin_addresses(self) -> set[str]:
        return {x.strip().lower() for x in self.ADMIN_ADDRESSES.split(',') if x.strip()}

    @property
    def superadmin_addresses(self) -> set[str]:
        return {x.strip().lower() for x in self.SUPERADMIN_ADDRESSES.split(',') if x.strip()}

    @property
    def master_network(self) -> Network:
        # The strategy source is always the real Hyperliquid MAINNET wallet.
        # HYPERLIQUID_MASTER_NETWORK is retained only so old deployments keep
        # parsing cleanly; its value no longer controls runtime behavior.
        return 'mainnet'

    @property
    def follower_network(self) -> Network:
        return self.HYPERLIQUID_FOLLOWER_NETWORK or self.HYPERLIQUID_NETWORK

    @staticmethod
    def hyperliquid_url_for(network: Network) -> str:
        return 'https://api.hyperliquid-testnet.xyz' if network == 'testnet' else 'https://api.hyperliquid.xyz'

    @property
    def hyperliquid_master_api_url(self) -> str:
        return self.hyperliquid_url_for(self.master_network)

    @property
    def hyperliquid_follower_api_url(self) -> str:
        return self.hyperliquid_url_for(self.follower_network)

    @property
    def hyperliquid_master_ws_url(self) -> str:
        return self.hyperliquid_master_api_url.replace('https://', 'wss://') + '/ws'

    @property
    def hyperliquid_follower_ws_url(self) -> str:
        return self.hyperliquid_follower_api_url.replace('https://', 'wss://') + '/ws'

    # Backward-compatible aliases: generic adapters are follower-side by
    # default because only followers ever sign orders.
    @property
    def hyperliquid_api_url(self) -> str:
        return self.hyperliquid_follower_api_url

    @property
    def hyperliquid_ws_url(self) -> str:
        return self.hyperliquid_follower_ws_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
