from __future__ import annotations

import argparse

import pytest

from app.security.risex_deployment_preflight_cli import (
    build_parser,
    verdict_exit_code,
)


def test_cli_is_testnet_only_and_exposes_no_secret_inputs() -> None:
    parser = build_parser()
    args = parser.parse_args([])

    assert args.network == 'testnet'
    destinations = {action.dest for action in parser._actions}
    forbidden = {
        'private_key',
        'secret',
        'signature',
        'token',
        'bearer',
        'jwt',
        'api_key',
        'operatorhub',
    }
    assert destinations.isdisjoint(forbidden)

    with pytest.raises(SystemExit):
        parser.parse_args(['--network', 'mainnet'])


def test_cli_allows_public_endpoint_overrides_only() -> None:
    args = build_parser().parse_args(
        [
            '--base-url',
            'https://api.example.test',
            '--rpc-url',
            'https://rpc.example.test',
        ]
    )

    assert args.base_url == 'https://api.example.test'
    assert args.rpc_url == 'https://rpc.example.test'


def test_preflight_exit_codes_keep_unknown_distinct_from_pass() -> None:
    assert verdict_exit_code('PASS') == 0
    assert verdict_exit_code('FAIL') == 1
    assert verdict_exit_code('UNKNOWN') == 2


def test_parser_description_states_read_only_behavior() -> None:
    parser: argparse.ArgumentParser = build_parser()

    assert 'read-only' in parser.description.lower()
    assert 'write' in parser.description.lower()
