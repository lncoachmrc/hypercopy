from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from pydantic import ValidationError

import app.api.user as user_api
import app.schemas.user as user_schemas
from app.api.deps import require_csrf


def _provider_route() -> APIRoute:
    route = next(
        (
            candidate
            for candidate in user_api.router.routes
            if isinstance(candidate, APIRoute) and candidate.path == '/trading-provider'
        ),
        None,
    )
    assert route is not None, 'PUT /trading-provider route is missing'
    assert 'PUT' in route.methods
    return route


def _provider_schema():
    schema = getattr(user_schemas, 'TradingProviderIn', None)
    assert schema is not None, 'TradingProviderIn schema is missing'
    return schema


def test_trading_provider_schema_requires_explicit_supported_provider() -> None:
    schema = _provider_schema()

    assert schema(provider='hyperliquid').provider == 'hyperliquid'
    assert schema(provider='risex').provider == 'risex'

    with pytest.raises(ValidationError):
        schema()
    with pytest.raises(ValidationError):
        schema(provider='other')
    with pytest.raises(ValidationError):
        schema(provider='RISEX')


def test_trading_provider_route_is_owner_scoped_and_csrf_protected() -> None:
    route = _provider_route()
    signature = inspect.signature(route.endpoint)

    assert 'user_id' not in signature.parameters
    assert {'body', 'user', 'db'} <= set(signature.parameters)
    assert any(dependency.call is require_csrf for dependency in route.dependant.dependencies)


def test_trading_provider_route_delegates_switch_authority_to_central_boundary() -> None:
    source = inspect.getsource(_provider_route().endpoint)

    assert 'set_user_destination' in source
    assert 'assess_destination_switch' not in source
    assert 'destination_switch_blockers' not in source
    assert '_network_switch_status' not in source


def test_trading_provider_route_cannot_reach_execution_writes() -> None:
    source = inspect.getsource(_provider_route().endpoint)

    assert 'RISExAdapter' not in source
    assert 'place_ioc' not in source
    assert 'publish_job' not in source
    assert 'RISEX_SIGNED_WRITES_ENABLED' not in source


def test_user_serialization_exposes_provider_and_local_readiness_without_live_provider_reads() -> None:
    source = inspect.getsource(user_api._serialize_user)

    assert "'execution_provider'" in source
    assert "'execution_network'" in source
    assert "'destination_switch_ready'" in source
    assert "'destination_switch_blockers'" in source
    assert 'destination_switch_blockers(' in source
    assert 'assess_destination_switch' not in source
    assert 'frontend_open_orders' not in source
    assert '.user_state(' not in source


@pytest.mark.asyncio
async def test_master_source_provider_switch_is_rejected_before_db_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = _provider_schema()
    endpoint = _provider_route().endpoint
    monkeypatch.setattr(user_api, 'is_master_source_user', lambda _user: True)
    user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc_info:
        await endpoint(schema(provider='risex'), user=user, db=None)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == user_api.MASTER_SOURCE_FOLLOWER_BLOCK_REASON
