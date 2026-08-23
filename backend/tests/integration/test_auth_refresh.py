import os

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

from app.core.config import settings
from app.core.security import hash_refresh_token
from app.db.redis import redis_client
from app.db.session import engine
from app.main import app


pytestmark = pytest.mark.skipif(os.getenv('RUN_INTEGRATION') != '1', reason='integration tests disabled')
AUTH = '/api/v1/auth'


@pytest.mark.asyncio
async def test_wallet_login_refresh_rotation_logout_and_cross_site_guard():
    account = Account.create()
    address = account.address.lower()
    transport = httpx.ASGITransport(app=app)

    try:
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

            # A cross-site form cannot rotate the HttpOnly credential because
            # the endpoint also requires a non-simple custom request header.
            rejected = await client.post(f'{AUTH}/refresh')
            assert rejected.status_code == 403
            assert client.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME) == first_refresh

            # Simulate an expired one-hour access session while the 24-hour
            # refresh family is still valid. No second wallet signature occurs.
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

            # If the first refresh response had been lost, a retry carrying the
            # predecessor must recover the exact same successor during grace.
            async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as delivery_retry:
                delivery_retry.cookies.set(settings.SESSION_REFRESH_COOKIE_NAME, first_refresh)
                recovered = await delivery_retry.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
                assert recovered.status_code == 200
                assert delivery_retry.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME) == second_refresh

            # Model a copied refresh credential being rotated elsewhere. A
            # second delivery retry of that same predecessor must recover the
            # same third token rather than create a divergent branch.
            async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as copied:
                copied.cookies.set(settings.SESSION_REFRESH_COOKIE_NAME, second_refresh)
                copied_rotation = await copied.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
                assert copied_rotation.status_code == 200
                third_refresh = copied.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME)
                assert third_refresh and third_refresh != second_refresh

                async with httpx.AsyncClient(transport=transport, base_url='http://traxion.test') as retry_again:
                    retry_again.cookies.set(settings.SESSION_REFRESH_COOKIE_NAME, second_refresh)
                    same_rotation = await retry_again.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
                    assert same_rotation.status_code == 200
                    assert retry_again.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME) == third_refresh

                # The original browser still has a valid access JWT carrying
                # the family id. Logout must revoke every access/refresh
                # credential from that family, including those held elsewhere.
                session = await client.get(f'{AUTH}/session')
                assert session.status_code == 200
                csrf = session.json()['csrf_token']

                logged_out = await client.post(
                    f'{AUTH}/logout',
                    headers={'X-Requested-With': 'HyperCopy', 'X-CSRF-Token': csrf},
                )
                assert logged_out.status_code == 204
                assert client.cookies.get(settings.SESSION_REFRESH_COOKIE_NAME) is None

                copied_access_after_logout = await copied.get(f'{AUTH}/session')
                assert copied_access_after_logout.status_code == 401

                copied.cookies.set(settings.SESSION_REFRESH_COOKIE_NAME, third_refresh)
                after_logout = await copied.post(f'{AUTH}/refresh', headers={'X-Requested-With': 'HyperCopy'})
                assert after_logout.status_code == 401
    finally:
        # These application singletons keep async connection pools. Dispose the
        # test connections before pytest-asyncio closes this function's loop so
        # later integration tests can safely open fresh connections on theirs.
        redis = redis_client()
        await redis.aclose()
        redis_client.cache_clear()
        await engine.dispose()
