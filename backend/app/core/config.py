from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', case_sensitive=True, extra='ignore')

    APP_ENV: Literal['development', 'staging', 'production'] = 'development'
    APP_VERSION: str = 'dev'
    LOG_LEVEL: str = 'INFO'
    PUBLIC_APP_URL: str = 'http://localhost:5173'
    API_BASE_URL: str = 'http://localhost:8000'

    DATABASE_URL: str = 'postgresql+asyncpg://hypercopy:hypercopy@postgres:5432/hypercopy'
    REDIS_URL: str = 'redis://redis:6379/0'

    HYPERLIQUID_NETWORK: Literal['testnet', 'mainnet'] = 'testnet'
    HYPERLIQUID_MASTER_ADDRESS: str = ''
    HL_RATE_BUDGET_PER_MIN: int = 1200
    HL_ORDER_EXPIRES_AFTER_MS: int = 15_000
    HL_AGENT_NAME: str = 'hypercopy'
    HL_MARKET_CACHE_TTL_SECONDS: int = 300
    HL_REPLAY_MAX_FILLS: int = 10_000

    ENABLE_LIVE_TRADING: bool = False
    DEFAULT_SHADOW_MODE: bool = True

    SESSION_SECRET: str = 'development-only-change-me'
    SESSION_TTL_SECONDS: int = 3600
    SESSION_COOKIE_NAME: str = 'hc_session'
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
            if self.KEK_PROVIDER == 'env' and self.HYPERLIQUID_NETWORK == 'mainnet':
                raise ValueError('Mainnet production requires an external KMS provider')
        if self.ENABLE_LIVE_TRADING and self.HYPERLIQUID_NETWORK != 'mainnet':
            raise ValueError('ENABLE_LIVE_TRADING is only meaningful on mainnet')
        if self.WATCHER_LEASE_RENEW_SECONDS >= self.WATCHER_LEASE_TTL_SECONDS:
            raise ValueError('watcher lease renew interval must be lower than TTL')
        return self

    @property
    def admin_addresses(self) -> set[str]:
        return {x.strip().lower() for x in self.ADMIN_ADDRESSES.split(',') if x.strip()}

    @property
    def superadmin_addresses(self) -> set[str]:
        return {x.strip().lower() for x in self.SUPERADMIN_ADDRESSES.split(',') if x.strip()}

    @property
    def hyperliquid_api_url(self) -> str:
        return 'https://api.hyperliquid-testnet.xyz' if self.HYPERLIQUID_NETWORK == 'testnet' else 'https://api.hyperliquid.xyz'

    @property
    def hyperliquid_ws_url(self) -> str:
        return self.hyperliquid_api_url.replace('https://', 'wss://') + '/ws'


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
