from __future__ import annotations

import json
import urllib.request

from eth_abi import decode, encode
from eth_utils import keccak


API_BASE = "https://api.testnet.rise.trade"
RPC_URL = "https://testnet.riselabs.xyz"
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
ZERO_ADDRESS = "0x" + "00" * 20


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "TRAXION-RISEx-readonly-probe/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _rpc(method: str, params: list[object]) -> object:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        RPC_URL,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "TRAXION-RISEx-readonly-probe/1"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict) and "error" not in payload, payload.get("error")
    return payload.get("result")


def _unwrap(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _address_from_slot(raw: object) -> str:
    assert isinstance(raw, str) and raw.startswith("0x")
    address = "0x" + raw[2:].rjust(64, "0")[-40:]
    assert int(address[2:], 16) != 0
    return address


def _eth_call(to: str, data: bytes, block: str) -> bytes:
    result = _rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
    assert isinstance(result, str) and result.startswith("0x")
    return bytes.fromhex(result[2:])


def test_capture_live_authorization_abi_compatibility() -> None:
    domain = _unwrap(_get_json(f"{API_BASE}/v1/auth/eip712-domain"))
    system = _unwrap(_get_json(f"{API_BASE}/v1/system/config"))
    deployed_address = domain.get("verifying_contract", domain.get("verifyingContract"))
    assert isinstance(deployed_address, str) and len(deployed_address) == 42

    block = _rpc("eth_blockNumber", [])
    assert isinstance(block, str)
    implementation = _address_from_slot(
        _rpc("eth_getStorageAt", [deployed_address, EIP1967_IMPLEMENTATION_SLOT, block])
    )
    code = _rpc("eth_getCode", [implementation, block])
    assert isinstance(code, str) and code.startswith("0x")
    code_bytes = bytes.fromhex(code[2:])

    signatures = {
        "eip712Domain": "eip712Domain()",
        "getSessionKeyStatus": "getSessionKeyStatus(address,address)",
        "hasPermission": "hasPermission(address,address,uint8)",
        "registerSigner": "registerSigner(address,address,string,uint48,uint8,uint32,bytes)",
    }
    selector_presence = {
        name: (selector := _selector(signature)).hex() in code[2:].lower()
        for name, signature in signatures.items()
    }

    domain_output = _eth_call(deployed_address, _selector(signatures["eip712Domain"]), block)
    fields, name, version, chain_id, verifying_contract, salt, extensions = decode(
        ["bytes1", "string", "string", "uint256", "address", "bytes32", "uint256[]"],
        domain_output,
    )

    session_call = _selector(signatures["getSessionKeyStatus"]) + encode(
        ["address", "address"], [ZERO_ADDRESS, ZERO_ADDRESS]
    )
    status_output = _eth_call(deployed_address, session_call, block)
    (status_value,) = decode(["uint8"], status_output)

    permission_call = _selector(signatures["hasPermission"]) + encode(
        ["address", "address", "uint8"], [ZERO_ADDRESS, ZERO_ADDRESS, 2]
    )
    permission_output = _eth_call(deployed_address, permission_call, block)
    (permission_value,) = decode(["bool"], permission_output)

    addresses = system.get("addresses") if isinstance(system.get("addresses"), dict) else {}
    system_auth = addresses.get("auth") if isinstance(addresses, dict) else None
    system_router = addresses.get("router") if isinstance(addresses, dict) else None

    evidence = {
        "block_number_hex": block,
        "implementation_code_bytes": len(code_bytes),
        "domain_call": {
            "ok": True,
            "fields_hex": "0x" + fields.hex(),
            "name": name,
            "version": version,
            "chain_id": chain_id,
            "verifying_contract_matches_api": verifying_contract.lower() == deployed_address.lower(),
            "salt_zero": int.from_bytes(salt, "big") == 0,
            "extensions": list(extensions),
        },
        "get_session_key_status_call": {"ok": True, "zero_account_status": status_value},
        "has_permission_call": {"ok": True, "permission_id": 2, "zero_account_value": permission_value},
        "selector_presence_in_implementation": selector_presence,
        "system_auth_matches_domain": isinstance(system_auth, str) and system_auth.lower() == deployed_address.lower(),
        "system_router_present": isinstance(system_router, str) and len(system_router) == 42,
        "signatures": {name: "0x" + _selector(signature).hex() for name, signature in signatures.items()},
    }

    raise AssertionError("TRAXION_RISEX_ABI_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
