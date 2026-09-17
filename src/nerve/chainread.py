from __future__ import annotations

from typing import Any, Protocol

from eth_abi.abi import decode, encode
from web3 import Web3
from web3.types import RPCEndpoint

from .rpc import RetryingHTTPProvider

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
MAX_UINT256 = 2**256 - 1

# Every method a read-only node may issue. Anything that signs or broadcasts is
# absent by construction, so a bug cannot turn a simulation into a transaction.
READ_METHODS = frozenset({
    "eth_blockNumber", "eth_call", "eth_simulateV1", "eth_getCode", "eth_getStorageAt",
    "eth_getLogs", "eth_getTransactionCount", "eth_getTransactionReceipt", "eth_chainId",
})


class RpcError(RuntimeError):
    pass


class ChainReader(Protocol):
    """Raw JSON-RPC reads. Tests replace it with a deterministic fake."""

    def request(self, method: str, params: list[Any]) -> Any: ...


class JsonRpcReader:
    """Read-only JSON-RPC client. It refuses any method outside READ_METHODS."""

    def __init__(self, rpc_url: str, timeout: int = 10) -> None:
        self.provider = RetryingHTTPProvider(rpc_url, request_kwargs={"timeout": timeout})

    def request(self, method: str, params: list[Any]) -> Any:
        if method not in READ_METHODS:
            raise RpcError(f"{method} is not a read method; this client never signs or sends")
        response = self.provider.make_request(RPCEndpoint(method), params)
        if response.get("error"):
            raise RpcError(f"{method}: {response['error']}")
        return response.get("result")


def selector(signature: str) -> bytes:
    return bytes(Web3.keccak(text=signature)[:4])


def calldata(signature: str, types: list[str] | None = None, args: list[Any] | None = None) -> str:
    return "0x" + (selector(signature) + encode(types or [], args or [])).hex()


def to_bytes(value: str | None) -> bytes:
    if not value or value == "0x":
        return b""
    return bytes.fromhex(value[2:] if value.startswith("0x") else value)


def decode_uint(data: bytes) -> int:
    if len(data) < 32:
        raise ValueError("return data shorter than one word")
    return int(decode(["uint256"], data[:32])[0])


def decode_address(data: bytes) -> str:
    if len(data) < 32:
        raise ValueError("return data shorter than one word")
    return str(decode(["address"], data[:32])[0]).lower()


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def address_topic(address: str) -> str:
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def eth_call(reader: ChainReader, to: str, data: str, block: int) -> bytes:
    return to_bytes(reader.request("eth_call", [{"to": to, "data": data}, hex(block)]))


def get_logs(reader: ChainReader, address: str, topics: list[str | None], from_block: int,
             to_block: int, chunk: int) -> list[dict[str, Any]]:
    """Bounded, chunked log read. The caller owns the window; this never scans from genesis."""
    logs: list[dict[str, Any]] = []
    start = max(0, from_block)
    while start <= to_block:
        end = min(to_block, start + chunk - 1)
        result = reader.request("eth_getLogs", [{"address": address, "topics": topics,
                                                 "fromBlock": hex(start), "toBlock": hex(end)}])
        logs.extend(result or [])
        start = end + 1
    return logs
