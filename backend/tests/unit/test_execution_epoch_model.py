import uuid

from sqlalchemy import inspect

from app.models.entities import ExecutionEpoch, User


def test_execution_epoch_is_mapped_with_immutable_destination_identity_fields():
    columns = inspect(ExecutionEpoch).columns
    assert {'id', 'user_id', 'provider', 'network', 'account_address', 'credential_version', 'started_at', 'ended_at'} <= set(columns.keys())


def test_user_maps_execution_provider_and_active_epoch_reference():
    columns = inspect(User).columns
    assert 'execution_provider' in columns
    assert 'active_execution_epoch_id' in columns
    assert columns.execution_provider.default is not None


def test_execution_epoch_accepts_provider_network_and_user_identity():
    user_id = uuid.uuid4()
    epoch = ExecutionEpoch(
        user_id=user_id,
        provider='risex',
        network='mainnet',
        account_address='0xabc',
    )
    assert epoch.user_id == user_id
    assert epoch.provider == 'risex'
    assert epoch.network == 'mainnet'
