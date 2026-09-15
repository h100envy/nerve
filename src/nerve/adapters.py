from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from web3 import Web3

from .models import ChainName, PoolObservation
from .rpc import RetryingHTTPProvider
from .sources import PoolSource

FACTORY_ABI = [{"name": "getPool", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}]}]
FACTORY_EVENTS_ABI = [{"anonymous": False, "inputs": [
    {"indexed": True, "internalType": "address", "name": "token0", "type": "address"},
    {"indexed": True, "internalType": "address", "name": "token1", "type": "address"},
    {"indexed": True, "internalType": "uint24", "name": "fee", "type": "uint24"},
    {"indexed": False, "internalType": "int24", "name": "tickSpacing", "type": "int24"},
    {"indexed": False, "internalType": "address", "name": "pool", "type": "address"},
], "name": "PoolCreated", "type": "event"}]
POOL_ABI = [
    {"name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function", "inputs": []},
    {"name": "liquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function", "inputs": []},
    {"name": "slot0", "outputs": [{"type": "uint160"}, {"type": "int24"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "uint8"}, {"type": "bool"}], "stateMutability": "view", "type": "function", "inputs": []},
]
QUOTER_ABI = [{"name": "quoteExactInputSingle", "outputs": [{"type": "uint256"}, {"type": "uint160"}, {"type": "uint32"}, {"type": "uint256"}], "stateMutability": "nonpayable", "type": "function", "inputs": [{"name": "params", "type": "tuple", "components": [
    {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"}, {"name": "sqrtPriceLimitX96", "type": "uint160"},
]}]}]
ERC20_ABI = [
    {"name": "decimals", "outputs": [{"type": "uint8"}], "stateMutability": "view", "type": "function", "inputs": []},
    {"name": "symbol", "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function", "inputs": []},
]


class RobinhoodChainPoolSource(PoolSource):
    """Read-only Uniswap V3 pool source for an explicit token allowlist.

    Holder concentration, LP lock and volume are intentionally enrichment
    inputs. They come from an indexer; this source never invents them.
    """

    def __init__(self, rpc_url: str, token_allowlist: tuple[str, ...], weth: str, factory: str,
                 quoter: str,
                 fees: tuple[int, ...] = (100, 500, 3000, 10_000), weth_usd: Decimal = Decimal("0"),
                 allow_any_token: bool = False, scan_window_blocks: int = 600,
                 enrichment_path: Path | None = None,
                 enrichment: dict[str, dict[str, Any]] | None = None) -> None:
        self.w3 = Web3(RetryingHTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        self.tokens, self.weth, self.factory_address = token_allowlist, Web3.to_checksum_address(weth), factory
        self.fees, self.weth_usd = fees, weth_usd
        self.allow_any_token, self.scan_window_blocks = allow_any_token, scan_window_blocks
        self.enrichment_path, self.enrichment = enrichment_path, enrichment or {}
        self.factory = self.w3.eth.contract(address=Web3.to_checksum_address(factory), abi=FACTORY_ABI + FACTORY_EVENTS_ABI)
        self.quoter = self.w3.eth.contract(address=Web3.to_checksum_address(quoter), abi=QUOTER_ABI)

    def discover(self) -> list[Any]:
        self._load_enrichment()
        result: list[Any] = []
        pairs: list[tuple[str, str, int, int | None]] = []
        if self.tokens:
            for token in self.tokens:
                for fee in self.fees:
                    pool = self.factory.functions.getPool(self.weth, Web3.to_checksum_address(token), fee).call()
                    if int(pool, 16):
                        pairs.append((Web3.to_checksum_address(token), Web3.to_checksum_address(pool), fee, None))
                        break
        elif self.allow_any_token:
            latest = int(self.w3.eth.block_number)
            event_signature = Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
            logs = self.w3.eth.get_logs({"address": Web3.to_checksum_address(self.factory_address), "topics": [event_signature],
                                         "fromBlock": max(0, latest - self.scan_window_blocks), "toBlock": latest})
            for log in logs:
                decoded = self.factory.events.PoolCreated().process_log(log)
                token0, token1 = Web3.to_checksum_address(decoded["args"]["token0"]), Web3.to_checksum_address(decoded["args"]["token1"])
                if token0.lower() == self.weth.lower():
                    pairs.append((token1, Web3.to_checksum_address(decoded["args"]["pool"]), int(decoded["args"]["fee"]), int(log["blockNumber"])))
                elif token1.lower() == self.weth.lower():
                    pairs.append((token0, Web3.to_checksum_address(decoded["args"]["pool"]), int(decoded["args"]["fee"]), int(log["blockNumber"])))
        for token, pool, fee, creation_block in pairs:
            contract = self.w3.eth.contract(address=pool, abi=POOL_ABI)  # type: ignore[call-overload]
            token_contract = self.w3.eth.contract(address=token, abi=ERC20_ABI)  # type: ignore[call-overload]
            code = self.w3.eth.get_code(token)  # type: ignore[arg-type]
            if not code:
                continue
            decimals = int(token_contract.functions.decimals().call())
            symbol = str(token_contract.functions.symbol().call())[:32]
            liquidity = int(contract.functions.liquidity().call())
            slot0 = contract.functions.slot0().call()
            pool_token0 = str(contract.functions.token0().call()).lower()
            q96 = Decimal(2**96)
            sqrt_price = Decimal(int(slot0[0]))
            active_weth = (Decimal(liquidity) * (q96 / sqrt_price if pool_token0 == self.weth.lower() else sqrt_price / q96)) / Decimal(10**18) if sqrt_price else Decimal("0")
            extra = self.enrichment.get(token.lower(), {})
            buy_route, sell_route, slippage_bps = self._route_metrics(token, fee, decimals)
            pool_age = Decimal("0")
            if creation_block is not None:
                created = self.w3.eth.get_block(creation_block)["timestamp"]
                pool_age = Decimal(str(max(0, datetime.now(UTC).timestamp() - int(created)) / 3600))
            observation = PoolObservation(chain=ChainName.ROBINHOOD, token=token, pool=pool,
                liquidity_usd=active_weth * self.weth_usd, volume_1h_usd=Decimal(str(extra.get("volume_1h_usd", 0))),
                volume_24h_usd=Decimal(str(extra.get("volume_24h_usd", 0))), top10_pct=Decimal(str(extra.get("top10_pct", 100))),
                slippage_bps=int(extra.get("slippage_bps", slippage_bps)), pool_age_hours=Decimal(str(extra.get("pool_age_hours", pool_age))),
                mint_renounced=extra.get("mint_renounced"), lp_locked=extra.get("lp_locked"),
                contract_verified=bool(extra.get("contract_verified", False)), buy_route=buy_route, sell_route=sell_route,
                metadata={"fee": fee, "symbol": symbol, "decimals": decimals, "bytecode_present": True,
                          "active_liquidity_weth": str(active_weth), "liquidity_raw": liquidity})
            result.append(observation.to_impulse())
        return result

    def _load_enrichment(self) -> None:
        if self.enrichment_path and self.enrichment_path.exists():
            payload = json.loads(self.enrichment_path.read_text())
            if not isinstance(payload, dict):
                raise ValueError("ENRICHMENT_PATH must contain an object keyed by token address")
            self.enrichment = {str(key).lower(): value for key, value in payload.items() if isinstance(value, dict)}

    def _route_metrics(self, token: str, fee: int, decimals: int) -> tuple[bool, bool, int]:
        probe_in = 10**15  # 0.001 WETH
        trade_in = 10**16  # 0.01 WETH
        try:
            small = int(self.quoter.functions.quoteExactInputSingle((self.weth, token, probe_in, fee, 0)).call()[0])
            full = int(self.quoter.functions.quoteExactInputSingle((self.weth, token, trade_in, fee, 0)).call()[0])
            if not small or not full:
                return False, False, 10_000
            ideal = Decimal(small) * Decimal(trade_in) / Decimal(probe_in)
            impact = max(Decimal("0"), Decimal("1") - Decimal(full) / ideal)
            sell = int(self.quoter.functions.quoteExactInputSingle((token, self.weth, max(full // 10, 1), fee, 0)).call()[0])
            return True, sell > 0, int(impact * 10_000)
        except Exception:
            return False, False, 10_000
