from __future__ import annotations

import inspect
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.routing import APIRoute
from sqlalchemy import Integer

from app.api import deps as api_deps
from app.api import user as user_api
from app.db.schema import EXPECTED_REVISION
from app.models import entities
from app.schemas import user as user_schemas


def _require(module: object, name: str):
    value = getattr(module, name, None)
    assert value is not None, f"RED: expected {module.__name__}.{name}"
    return value


def test_risex_models_are_parallel_and_hyperliquid_models_stay_structurally_unchanged() -> None:
    risex_account = _require(entities, "RISExTradingAccount")
    risex_credential = _require(entities, "RISExSigningCredential")

    assert risex_account.__tablename__ == "risex_trading_accounts"
    assert risex_credential.__tablename__ == "risex_signing_credentials"

    hyperliquid_account_columns = set(entities.TradingAccount.__table__.c.keys())
    hyperliquid_credential_columns = set(entities.SigningCredential.__table__.c.keys())

    assert hyperliquid_account_columns == {
        "id",
        "user_id",
        "account_address",
        "agent_address",
        "agent_name",
        "verified_at",
        "created_at",
        "updated_at",
    }
    assert hyperliquid_credential_columns == {
        "id",
        "trading_account_id",
        "ciphertext_b64",
        "nonce_b64",
        "wrapped_dek_b64",
        "wrap_nonce_b64",
        "key_provider",
        "key_reference",
        "key_version",
        "agent_fingerprint",
        "expires_at",
        "status",
        "created_at",
        "updated_at",
    }
    assert "provider" not in hyperliquid_account_columns
    assert "provider" not in hyperliquid_credential_columns


def test_risex_logical_generation_is_distinct_from_envelope_key_version() -> None:
    credential_type = _require(entities, "RISExSigningCredential")
    columns = credential_type.__table__.c

    assert "generation" in columns, (
        "RED: RISEx credential needs a logical generation counter that rotates per user"
    )
    assert "key_version" in columns, (
        "RED: envelope-format key_version must remain separate from logical generation"
    )
    assert isinstance(columns.generation.type, Integer)
    assert columns.generation.nullable is False

    default = columns.generation.default
    server_default = columns.generation.server_default
    default_value = getattr(default, "arg", None)
    server_default_text = str(getattr(server_default, "arg", "")) if server_default else ""
    normalized_server_default = server_default_text.replace("'", "").replace('"', "").strip()
    assert default_value == 1 or normalized_server_default == "1", (
        "RED: logical credential generation must start at 1"
    )


def test_0018_migration_exists_is_head_and_is_additive_only() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    migration = backend_root / "alembic" / "versions" / "0018_risex_user_credentials.py"

    assert migration.exists(), "RED: migration 0018_risex_user_credentials.py is missing"
    source = migration.read_text()

    assert 'revision = "0018_risex_user_credentials"' in source
    assert 'down_revision = "0017_master_event_causal_order"' in source
    assert "op.create_table(" in source
    assert '"risex_trading_accounts"' in source or "'risex_trading_accounts'" in source
    assert '"risex_signing_credentials"' in source or "'risex_signing_credentials'" in source

    upgrade = source.split("def upgrade() -> None:", 1)[1].split("def downgrade() -> None:", 1)[0]
    for forbidden in (
        "op.add_column(",
        "op.alter_column(",
        "op.rename_table(",
        "op.drop_column(",
        "op.drop_table(",
        "op.execute(",
    ):
        assert forbidden not in upgrade, (
            f"RED: 0018 must be additive-only and create only new RISEx tables/indexes; found {forbidden}"
        )


def test_0018_is_registered_as_runtime_expected_revision() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    alembic_config = Config(str(backend_root / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(backend_root / "alembic"))
    actual_head = ScriptDirectory.from_config(alembic_config).get_current_head()
    assert EXPECTED_REVISION == actual_head


def test_0018_is_registered_in_targeted_release_preflight() -> None:
    repo_root = Path(__file__).resolve().parents[3]
    preflight = (repo_root / "scripts" / "targeted_release_preflight.py").read_text()

    assert "'0018_risex_user_credentials.py'" in preflight, (
        "RED: scripts/targeted_release_preflight.py must register migration 0018"
    )


def test_risex_request_schema_contains_only_account_and_signer_private_key() -> None:
    schema = _require(user_schemas, "RISExTradingAccountIn")
    assert set(schema.model_fields) == {"account_address", "signer_private_key"}
    assert "signer_address" not in schema.model_fields
    assert "network" not in schema.model_fields

    assert set(user_schemas.TradingAccountIn.model_fields) == {
        "agent_address",
        "account_address",
        "agent_private_key",
    }


def test_risex_post_route_is_parallel_csrf_protected_and_current_user_scoped() -> None:
    endpoint = _require(user_api, "link_risex_trading_account")
    routes = [
        route
        for route in user_api.router.routes
        if isinstance(route, APIRoute)
        and route.path == "/risex-trading-account"
        and "POST" in route.methods
    ]
    assert len(routes) == 1, "RED: POST /risex-trading-account route is missing"

    dependency_calls = {
        dependency.call
        for dependency in routes[0].dependant.dependencies
        if dependency.call is not None
    }
    assert api_deps.require_csrf in dependency_calls
    assert api_deps.current_user in dependency_calls

    source = inspect.getsource(endpoint)
    assert re.search(r"\bTradingAccountIn\b", source) is None, (
        "RED: RISEx endpoint must use its own input schema and leave Hyperliquid endpoint untouched"
    )


def test_risex_endpoint_contract_reuses_authorization_collector_and_logical_generation() -> None:
    endpoint = _require(user_api, "link_risex_trading_account")
    source = inspect.getsource(endpoint)

    assert "_verify_risex_signer_binding" in source, (
        "RED: endpoint must verify the exact account/signer pair on-chain before persistence"
    )
    assert "crypto.encrypt" in source, "RED: RISEx signer private key must be envelope-encrypted"
    assert "set_user_destination" in source, (
        "RED: successful linking must bind the verified RISEx identity to a fresh execution epoch"
    )
    assert "generation" in source, (
        "RED: logical credential generation must become ExecutionEpoch.credential_version"
    )
    assert "blob.key_version" not in source, (
        "RED: envelope key_version must never be used as RISEx credential_version"
    )
