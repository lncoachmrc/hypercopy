import os

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings
from app.core.security import hash_refresh_token
from app.db.redis import redis_client
from app.main import app


pytestmark = pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='integration tests disabled')
AUTH = '/api/v1/auth'


@pytest.mark.asyncio
async def test_wallet_login_refresh_rotation_and_logout_revocation():
    account = Account.create()
    address = account.address.lower()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as client:
        challenge = await client.post(f'{AUTH}/challenge', json={'address': address})
        assert challenge.status_code == 200
        message = challenge.json()['message']
        signature = Account.sign_message(encode_defunct(text=message), account.key).signature.hex()

        verified = await client.post(f'{AUTH}/verify', json={'address': address, 'signature': signature})
        assert verified.status_code == 200
        assert verified.json()['user']['auth_wallet'] == address
        first_refresh = client.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME)
        assert first_refresh
        assert client.cookies.get(settings.SESSION_COOKIE_NAME)
        assert client.cookies.get(settings.CSRF_COOKIE_NAME)

        # Simulate an expired one-hour access session while the 24-hour refresh
        # family is still valid. Refresh must work without another wallet sign.
        client.cookies.delete(settings.SESSION_COOKIE_NAME)
        client.cookies.delete(settings.CSRF_COOKIE_NAME)
        refreshed = await client.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
        assert refreshed.status_code == 200
        second_refresh = client.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME)
        assert second_refresh and second_refresh != first_refresh
        assert client.cookies.get(settings.SESSION_COOKIE_NAME)
        assert client.cookies.get(settings.CSRF_COOKIE_NAME)

        remaining = await redis_client().ttl(f'session:refresh:{hash_refresh_token(second_refresh)}')
        assert 0 < remaining <= settings.SESSION_REFRESH_TTL_SECONDS
        assert remaining > settings.SESSION_REFRESH_TTL_SECONDS - 120

        # Refresh credentials are one-time: replaying the rotated-out token is rejected.
        async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as stale:
            stale.cookies.set(settings.SESSION_REFRESH_COOKIE_NAME, first_refresh)
            replay = await stale.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
            assert replay.status_code == 401

        session = await client.get(f'{AUTH}/session')
        assert session.status_code == 200
        csrf = session.json()['csrf_token']

        logged_out = await client.post(
            f'{AUTH}/logout',
            headers={'X-Requested-With': 'HyperCopy', 'X-CSRF-Token': csrf},
        )
        assert logged_out.status_code == 204
        assert client.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME) is None

        async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as revoked:
            revoked.cookies.set(settings.SESSION_REFRESH_COOKIE_NAME, second_refresh)
            after_logout = await revoked.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
            assert after_logout.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rejects_cross_site_style_request_without_custom_header():
    account = Account.create()
    address = account.address.lower()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as client:
        challenge = await client.post(f'{AUTH}/challenge', json={'address': address})
        message = challenge.json()['message']
        signature = Account.sign_message(encode_defunct(text=message), account.key).signature.hex()
        verified = await client.post(f'{AUTH}/verify', json={'address': address, 'signature': signature})
        assert verified.status_code == 200

        rejected = await client.post(f'{AUTH}/refresh')
        assert rejected.status_code == 403
