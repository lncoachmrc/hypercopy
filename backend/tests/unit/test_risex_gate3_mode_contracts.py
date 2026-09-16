from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.risex import RISExAdapter


def test_short_lived_mode_rejects_continuous_window_material_without_fallback() -> None:
    continuous_material = SimpleNamespace(
        window=object(),
        account_address='0x' + ('11' * 20),
        signer_address='0x' + ('22' * 20),
        context_fingerprint='test-context',
    )

    with pytest.raises(
        ValueError,
        match='short-lived|continuous|authorization material|gate.?3',
    ):
        RISExAdapter(
            network='testnet',
            gate3_mode='short_lived_attestation',
            continuous_authorization=continuous_material,
        )
