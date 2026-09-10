from __future__ import annotations

import json
import urllib.parse
import urllib.request

from eth_utils import keccak


API_BASE = "https://api.testnet.rise.trade"
RPC_URL = "https://testnet.riselabs.xyz"
EXPLORER_API = "https://explorer.testnet.riselabs.xyz/api"
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
EIP1967_BEACON_SLOT = (
    "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
)


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "TRAXION-RISEx-readonly-probe/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        assert response.status == 200
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
        assert response.status == 200
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict) and "error" not in payload
    return payload.get("result")


def _unwrap(payload: dict) -> dict:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _first_address_by_key(payload: object, wanted: tuple[str, ...]) -> str | None:
    normalized = {key.replace("_", "").lower() for key in wanted}

    def walk(value: object) -> str | None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.replace("_", "").lower() in normalized and isinstance(child, str):
                    if child.startswith("0x") and len(child) == 42:
                        return child
            for child in value.values():
                found = walk(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = walk(child)
                if found:
                    return found
        return None

    return walk(payload)


def _address_from_slot(raw: object) -> str | None:
    if not isinstance(raw, str) or not raw.startswith("0x"):
        return None
    value = raw[2:].rjust(64, "0")[-40:]
    if int(value, 16) == 0:
        return None
    return "0x" + value


def _code_evidence(address: str, block: str) -> dict[str, object]:
    code = _rpc("eth_getCode", [address, block])
    assert isinstance(code, str) and code.startswith("0x")
    raw = bytes.fromhex(code[2:])
    implementation = _address_from_slot(
        _rpc("eth_getStorageAt", [address, EIP1967_IMPLEMENTATION_SLOT, block])
    )
    beacon = _address_from_slot(_rpc("eth_getStorageAt", [address, EIP1967_BEACON_SLOT, block]))
    result: dict[str, object] = {
        "address": address,
        "code_bytes": len(raw),
        "code_keccak256": "0x" + keccak(raw).hex(),
        "eip1967_implementation": implementation,
        "eip1967_beacon": beacon,
    }
    if implementation:
        impl_code = _rpc("eth_getCode", [implementation, block])
        assert isinstance(impl_code, str) and impl_code.startswith("0x")
        impl_raw = bytes.fromhex(impl_code[2:])
        result["implementation_code_bytes"] = len(impl_raw)
        result["implementation_code_keccak256"] = "0x" + keccak(impl_raw).hex()
    return result


def _explorer_summary(address: str) -> dict[str, object]:
    summary: dict[str, object] = {}
    for action in ("getsourcecode", "getabi"):
        query = urllib.parse.urlencode({"module": "contract", "action": action, "address": address})
        try:
            payload = _get_json(f"{EXPLORER_API}?{query}")
        except Exception as exc:  # diagnostic only: preserve UNKNOWN evidence
            summary[action] = {"available": False, "error": type(exc).__name__}
            continue
        if action == "getsourcecode":
            rows = payload.get("result")
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            summary[action] = {
                "available": bool(row),
                "contract_name": row.get("ContractName"),
                "compiler_version": row.get("CompilerVersion"),
                "proxy": row.get("Proxy"),
                "implementation": row.get("Implementation"),
                "source_present": bool(row.get("SourceCode")),
                "abi_present": bool(row.get("ABI")) and row.get("ABI") != "Contract source code not verified",
            }
        else:
            result = payload.get("result")
            summary[action] = {
                "available": isinstance(result, str) and result.startswith("[")
            }
    return summary


def test_capture_risex_runtime_deployment_evidence() -> None:
    domain_raw = _get_json(f"{API_BASE}/v1/auth/eip712-domain")
    system_raw = _get_json(f"{API_BASE}/v1/system/config")
    domain = _unwrap(domain_raw)
    system = _unwrap(system_raw)

    chain_id = domain.get("chain_id", domain.get("chainId"))
    auth = domain.get("verifying_contract", domain.get("verifyingContract"))
    router = _first_address_by_key(system, ("router",))
    system_auth = _first_address_by_key(system, ("auth", "authorization", "authorization_contract"))
    orders_manager = _first_address_by_key(system, ("orders_manager", "ordersManager"))
    perps_manager = _first_address_by_key(system, ("perps_manager", "perpsManager"))
    collateral_manager = _first_address_by_key(system, ("collateral_manager", "collateralManager"))
    operator_hub = _first_address_by_key(system, ("operator_hub", "operatorHub"))

    assert isinstance(chain_id, (int, str))
    assert isinstance(auth, str) and auth.startswith("0x") and len(auth) == 42
    assert isinstance(router, str) and router.startswith("0x") and len(router) == 42

    rpc_chain_id = _rpc("eth_chainId", [])
    block = _rpc("eth_blockNumber", [])
    assert isinstance(rpc_chain_id, str) and isinstance(block, str)

    addresses = {
        "auth": auth,
        "router": router,
        "system_auth": system_auth,
        "orders_manager": orders_manager,
        "perps_manager": perps_manager,
        "collateral_manager": collateral_manager,
        "operator_hub": operator_hub,
    }
    contracts = {
        name: {
            "code": _code_evidence(address, block),
            "explorer": _explorer_summary(address),
        }
        for name, address in addresses.items()
        if isinstance(address, str)
    }

    evidence = {
        "api_base": API_BASE,
        "rpc_url": RPC_URL,
        "chain_id_api": str(chain_id),
        "chain_id_rpc_hex": rpc_chain_id,
        "block_number_hex": block,
        "domain": {
            "name": domain.get("name"),
            "version": domain.get("version"),
            "verifying_contract": auth,
        },
        "addresses": addresses,
        "contracts": contracts,
    }

    raise AssertionError("TRAXION_RISEX_RUNTIME_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
