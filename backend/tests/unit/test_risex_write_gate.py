from __future__ import annotations

import pytest


class NoNetworkTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_json(self, path: str, *, params: dict | None = None) -> dict:
        self.calls.append(('GET', path))
        raise AssertionError('write gate must fail before network I/O')

    async def post_json(self, path: str, *, json: dict | None = None) -> dict:
        self.calls.append(('POST', path))
        raise AssertionError('write gate must fail before network I/O')


@pytest.mark.asyncio
async def test_every_risex_mutation_fails_locally_before_network_io() -> None:
    from app.adapters.risex import RISExAdapter
    from app.adapters.risex_types import ProviderWriteDisabled

    transport = NoNetworkTransport()
    adapter = RISExAdapter(network='testnet', transport=transport)

    assert adapter.provider == 'risex'
    assert adapter.writes_enabled is False

    write_calls = [
        lambda: adapter.place_ioc(
            account_address='0x' + '11' * 20,
            asset='BTC',
            is_buy=True,
            size='0.01',
            mark_price='60000',
            slippage_bps=25,
            reduce_only=False,
            cloid='1',
        ),
        lambda: adapter.update_leverage(
            account_address='0x' + '11' * 20,
            asset='BTC',
            leverage=2,
            is_cross=True,
        ),
        lambda: adapter.cancel_order(account_address='0x' + '11' * 20, order_id='123'),
        lambda: adapter.register_signer(account_address='0x' + '11' * 20, signer_address='0x' + '22' * 20),
        lambda: adapter.revoke_signer(account_address='0x' + '11' * 20, signer_address='0x' + '22' * 20),
        lambda: adapter.approve_builder_fee(account_address='0x' + '11' * 20, builder_id=1, max_fee_bps=1),
    ]

    for call in write_calls:
        with pytest.raises(ProviderWriteDisabled, match='RISEx writes are disabled'):
            await call()

    assert transport.calls == []
