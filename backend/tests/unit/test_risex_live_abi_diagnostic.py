from __future__ import annotations

import hashlib
import json
import urllib.request

from eth_utils import keccak


API_BASE = "https://api.testnet.rise.trade"
RPC_URL = "https://testnet.riselabs.xyz"
EIP1967_IMPLEMENTATION_SLOT = (
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
)
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "TRAXION-RISEx-readonly-probe/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _get_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "TRAXION-RISEx-readonly-probe/1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


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


def _address_from_slot(raw: object) -> str:
    assert isinstance(raw, str) and raw.startswith("0x")
    address = "0x" + raw[2:].rjust(64, "0")[-40:]
    assert int(address[2:], 16) != 0
    return address


def _base58(raw: bytes) -> str:
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    leading = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading + (encoded or "1")


def _metadata_multihash(code_bytes: bytes) -> tuple[bytes, bytes] | None:
    if len(code_bytes) < 2:
        return None
    metadata_len = int.from_bytes(code_bytes[-2:], "big")
    if metadata_len <= 0 or metadata_len + 2 > len(code_bytes):
        return None
    metadata = code_bytes[-2 - metadata_len : -2]
    marker = metadata.find(b"ipfs")
    if marker < 0:
        return None
    digest_marker = metadata.find(b"\x12\x20", marker)
    if digest_marker < 0 or digest_marker + 34 > len(metadata):
        return None
    return metadata, metadata[digest_marker : digest_marker + 34]


def _fetch_ipfs_metadata(multihash: bytes) -> dict[str, object]:
    cid = _base58(multihash)
    errors: list[str] = []
    raw: bytes | None = None
    gateway_used: str | None = None
    for base in (
        "https://ipfs.io/ipfs/",
        "https://gateway.pinata.cloud/ipfs/",
        "https://dweb.link/ipfs/",
    ):
        try:
            raw = _get_bytes(base + cid)
            gateway_used = base
            break
        except Exception as exc:
            errors.append(type(exc).__name__)
    if raw is None:
        return {"cid": cid, "retrieved": False, "gateway_errors": errors}

    digest_matches = multihash[:2] == b"\x12\x20" and hashlib.sha256(raw).digest() == multihash[2:]
    try:
        metadata = json.loads(raw.decode("utf-8"))
    except Exception:
        return {
            "cid": cid,
            "retrieved": True,
            "gateway": gateway_used,
            "sha256_matches_multihash": digest_matches,
            "json": False,
        }
    if not isinstance(metadata, dict):
        return {
            "cid": cid,
            "retrieved": True,
            "gateway": gateway_used,
            "sha256_matches_multihash": digest_matches,
            "json": False,
        }

    output = metadata.get("output") if isinstance(metadata.get("output"), dict) else {}
    abi = output.get("abi") if isinstance(output, dict) else None
    abi_entries = abi if isinstance(abi, list) else []
    function_names = sorted(
        {
            str(item.get("name"))
            for item in abi_entries
            if isinstance(item, dict) and item.get("type") == "function" and isinstance(item.get("name"), str)
        }
    )
    settings = metadata.get("settings") if isinstance(metadata.get("settings"), dict) else {}
    sources = metadata.get("sources") if isinstance(metadata.get("sources"), dict) else {}
    compiler = metadata.get("compiler") if isinstance(metadata.get("compiler"), dict) else {}
    return {
        "cid": cid,
        "retrieved": True,
        "gateway": gateway_used,
        "sha256_matches_multihash": digest_matches,
        "json": True,
        "compiler_version": compiler.get("version") if isinstance(compiler, dict) else None,
        "compilation_target": settings.get("compilationTarget") if isinstance(settings, dict) else None,
        "source_count": len(sources),
        "abi_entry_count": len(abi_entries),
        "function_names": function_names,
    }


def _implementation_evidence(proxy: str, block: str) -> dict[str, object]:
    implementation = _address_from_slot(
        _rpc("eth_getStorageAt", [proxy, EIP1967_IMPLEMENTATION_SLOT, block])
    )
    code = _rpc("eth_getCode", [implementation, block])
    assert isinstance(code, str) and code.startswith("0x")
    raw = bytes.fromhex(code[2:])
    located = _metadata_multihash(raw)
    result: dict[str, object] = {
        "implementation": implementation,
        "runtime_code_bytes": len(raw),
        "runtime_code_keccak256": "0x" + keccak(raw).hex(),
        "metadata_detected": located is not None,
    }
    if located is not None:
        metadata_cbor, multihash = located
        result["metadata_cbor_bytes"] = len(metadata_cbor)
        result["metadata_cbor_tail_hex"] = "0x" + metadata_cbor[-64:].hex()
        result["ipfs"] = _fetch_ipfs_metadata(multihash)
    return result


def test_capture_live_solidity_metadata_and_abi_binding() -> None:
    domain = _unwrap(_get_json(f"{API_BASE}/v1/auth/eip712-domain"))
    system = _unwrap(_get_json(f"{API_BASE}/v1/system/config"))
    addresses = system.get("addresses") if isinstance(system.get("addresses"), dict) else {}

    deployed_address = domain.get("verifying_contract", domain.get("verifyingContract"))
    router = addresses.get("router") if isinstance(addresses, dict) else None
    assert isinstance(deployed_address, str) and len(deployed_address) == 42
    assert isinstance(router, str) and len(router) == 42

    block = _rpc("eth_blockNumber", [])
    assert isinstance(block, str)

    evidence = {
        "block_number_hex": block,
        "authorization": _implementation_evidence(deployed_address, block),
        "router": _implementation_evidence(router, block),
    }
    raise AssertionError("TRAXION_RISEX_METADATA_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
