from __future__ import annotations

import base64
import os
from pathlib import Path
import subprocess
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]
_VALID_AUDIT_IP_HASH_KEY_B64 = base64.b64encode(b'a' * 32).decode()


def _production_env(
    *,
    session_secret: str | None = None,
    audit_ip_hash_key_b64: str | None = _VALID_AUDIT_IP_HASH_KEY_B64,
) -> dict[str, str]:
    env = os.environ.copy()
    env['APP_ENV'] = 'production'
    env['ENABLE_LIVE_TRADING'] = 'false'
    env['KEK_PROVIDER'] = 'env'
    env['PYTHONPATH'] = str(BACKEND_ROOT)
    if session_secret is None:
        env.pop('SESSION_SECRET', None)
    else:
        env['SESSION_SECRET'] = session_secret
    if audit_ip_hash_key_b64 is None:
        env.pop('AUDIT_IP_HASH_KEY_B64', None)
    else:
        env['AUDIT_IP_HASH_KEY_B64'] = audit_ip_hash_key_b64
    return env


def _import_module(
    module: str,
    *,
    session_secret: str | None = None,
    audit_ip_hash_key_b64: str | None = _VALID_AUDIT_IP_HASH_KEY_B64,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-c', f'import {module}'],
        cwd=BACKEND_ROOT,
        env=_production_env(
            session_secret=session_secret,
            audit_ip_hash_key_b64=audit_ip_hash_key_b64,
        ),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize(
    'module',
    [
        'app.workers.watcher',
        'app.workers.resilient_execution_worker',
        'app.workers.ai_intelligence_worker',
    ],
)
def test_production_worker_startup_does_not_require_api_secrets(module: str) -> None:
    result = _import_module(module, audit_ip_hash_key_b64=None)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('session_secret', [None, 'too-short'])
def test_production_api_startup_requires_strong_session_secret(session_secret: str | None) -> None:
    result = _import_module('app.main', session_secret=session_secret)

    assert result.returncode != 0
    assert 'SESSION_SECRET must be a strong production secret' in result.stderr


@pytest.mark.parametrize(
    'audit_ip_hash_key_b64',
    [
        None,
        'not-valid-base64!',
        base64.b64encode(b'a' * 31).decode(),
    ],
)
def test_production_api_startup_requires_strong_audit_ip_hash_key(audit_ip_hash_key_b64: str | None) -> None:
    result = _import_module(
        'app.main',
        session_secret='s' * 48,
        audit_ip_hash_key_b64=audit_ip_hash_key_b64,
    )

    assert result.returncode != 0
    assert 'AUDIT_IP_HASH_KEY_B64 must be an independent Base64 key decoding to at least 32 bytes' in result.stderr


def test_production_api_startup_accepts_strong_api_secrets() -> None:
    result = _import_module('app.main', session_secret='s' * 48)

    assert result.returncode == 0, result.stderr
