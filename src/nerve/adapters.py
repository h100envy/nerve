from __future__ import annotations

from decimal import Decimal
from typing import Any

from web3 import Web3

from .models import ChainName, PoolObservation
from .sources import PoolSource

FACTORY_ABI = [{"name": "getPool", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}]}]
POOL_ABI = [
    {"name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function", "inputs": []},
    {"name": "liquidity", "outputs": [{"type": "uint128"}], "stateMutability": "view", "type": "function", "inputs": []},
    {"name": "slot0", "outputs": [{"type": "uint160"}, {"type": "int24"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "uint8"}, {"type": "bool"}], "stateMutability": "view", "type": "function", "inputs": []},
]


class RobinhoodChainPoolSource(PoolSource):
    """Read-only Uniswap V3 pool source for an explicit token allowlist.

    Holder concentration, LP lock and volume are intentionally enrichment
    inputs. They come from an indexer; this source never invents them.
    """

    def __init__(self, rpc_url: str, token_allowlist: tuple[str, ...], weth: str, factory: str,
                 fees: tuple[int, ...] = (100, 500, 3000, 10_000), weth_usd: Decimal = Decimal("0"),
                 enrichment: dict[str, dict[str, Any]] | None = None) -> None:
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        self.tokens, self.weth, self.factory_address = token_allowlist, weth, factory
        self.fees, self.weth_usd, self.enrichment = fees, weth_usd, enrichment or {}
        self.factory = self.w3.eth.contract(address=Web3.to_checksum_address(factory), abi=FACTORY_ABI)

    def discover(self) -> list[Any]:
        result: list[Any] = []
        for token in self.tokens:
            found = None
            for fee in self.fees:
                pool = self.factory.functions.getPool(Web3.to_checksum_address(self.weth), Web3.to_checksum_address(token), fee).call()
                if int(pool, 16):
                    found = (Web3.to_checksum_address(pool), fee)
                    break
            if found is None:
                continue
            pool, fee = found
            contract = self.w3.eth.contract(address=pool, abi=POOL_ABI)
            liquidity = int(contract.functions.liquidity().call())
            slot0 = contract.functions.slot0().call()
            token0 = str(contract.functions.token0().call()).lower()
            q96 = Decimal(2**96)
            sqrt_price = Decimal(int(slot0[0]))
            active_weth = (Decimal(liquidity) * (q96 / sqrt_price if token0 == self.weth.lower() else sqrt_price / q96)) / Decimal(10**18) if sqrt_price else Decimal("0")
            extra = self.enrichment.get(token.lower(), {})
            observation = PoolObservation(chain=ChainName.ROBINHOOD, token=token, pool=pool,
                liquidity_usd=active_weth * self.weth_usd, volume_1h_usd=Decimal(str(extra.get("volume_1h_usd", 0))),
                volume_24h_usd=Decimal(str(extra.get("volume_24h_usd", 0))), top10_pct=Decimal(str(extra.get("top10_pct", 100))),
                slippage_bps=int(extra.get("slippage_bps", 10000)), pool_age_hours=Decimal(str(extra.get("pool_age_hours", 0))),
                mint_renounced=extra.get("mint_renounced"), lp_locked=extra.get("lp_locked"),
                contract_verified=bool(extra.get("contract_verified", False)), buy_route=True, sell_route=True,
                metadata={"fee": fee, "active_liquidity_weth": str(active_weth), "liquidity_raw": liquidity})
            result.append(observation.to_impulse())
        return result
