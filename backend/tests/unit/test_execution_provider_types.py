import importlib
import importlib.util
import uuid


def test_destination_identity_separates_provider_network_and_epoch():
    module_spec = importlib.util.find_spec('app.adapters.base')
    assert module_spec is not None, 'provider-neutral adapter types are missing'

    base = importlib.import_module('app.adapters.base')
    epoch_a = uuid.uuid4()
    epoch_b = uuid.uuid4()

    hyperliquid = base.DestinationIdentity(
        provider='hyperliquid',
        network='mainnet',
        epoch_id=epoch_a,
        account_address='0xabc',
    )
    risex_same_network = base.DestinationIdentity(
        provider='risex',
        network='mainnet',
        epoch_id=epoch_a,
        account_address='0xabc',
    )
    hyperliquid_new_epoch = base.DestinationIdentity(
        provider='hyperliquid',
        network='mainnet',
        epoch_id=epoch_b,
        account_address='0xabc',
    )

    assert hyperliquid != risex_same_network
    assert hyperliquid != hyperliquid_new_epoch
    assert hyperliquid.provider == 'hyperliquid'
    assert risex_same_network.provider == 'risex'
    assert hyperliquid.network == risex_same_network.network == 'mainnet'
