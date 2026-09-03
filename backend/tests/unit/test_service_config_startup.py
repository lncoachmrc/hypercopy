from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _production_env(*, session_secret: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env['APP_ENV'] = 'production'
    env['ENABLE_LIVE_TRADING'] = 'false'
    env['KEK_PROVIDER'] = 'env'
    env['PYTHONPATH'] = str(BACKEND_ROOT)
    if session_secret is None:
        env.pop('SESSION_SECRET', None)
    else:
        env['SESSION_SECRET'] = session_secret
    return env


def _import_module(module: str, *, session_secret: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-c', f'import {module}'],
        cwd=BACKEND_ROOT,
        env=_production_env(session_secret=session_secret),
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
def test_production_worker_startup_does_not_require_session_secret(module: str) -> None:
    result = _import_module(module)

    assert result.returncode == 0, result.stderr


def test_production_api_startup_requires_strong_session_secret() -> None:
    result = _import_module('app.main')

    assert result.returncode != 0
    assert 'SESSION_SECRET must be a strong production secret' in result.stderr


def test_production_api_startup_accepts_strong_session_secret() -> None:
    result = _import_module('app.main', session_secret='s' * 48)

    assert result.returncode == 0, result.stderr
