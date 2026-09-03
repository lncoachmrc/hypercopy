from __future__ import annotations

import base64
import hashlib
import hmac

from app.core import security


def _key_b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def test_hash_ip_uses_versioned_dedicated_hmac_and_not_session_secret(monkeypatch) -> None:
    key = b'k' * 32
    ip = '203.0.113.42'
    monkeypatch.setattr(security.settings, 'AUDIT_IP_HASH_KEY_B64', _key_b64(key))
    monkeypatch.setattr(security.settings, 'SESSION_SECRET', 's' * 48)

    first = security.hash_ip(ip)

    monkeypatch.setattr(security.settings, 'SESSION_SECRET', 't' * 48)
    second = security.hash_ip(ip)
    digest = hmac.new(key, b'traxion:audit-ip:v2:' + ip.encode(), hashlib.sha256).digest()
    expected = 'v2:' + base64.urlsafe_b64encode(digest).rstrip(b'=').decode()

    assert first == expected
    assert second == first
    assert len(expected) <= 64


def test_hash_ip_distinguishes_ips(monkeypatch) -> None:
    monkeypatch.setattr(security.settings, 'AUDIT_IP_HASH_KEY_B64', _key_b64(b'k' * 32))

    assert security.hash_ip('203.0.113.42') != security.hash_ip('203.0.113.43')


def test_hash_ip_none_stays_none() -> None:
    assert security.hash_ip(None) is None
