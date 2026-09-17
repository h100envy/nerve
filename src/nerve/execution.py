from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from web3 import Web3
from web3.exceptions import TimeExhausted

from .models import Impulse
from .rpc import RetryingHTTPProvider


class ExecutionError(RuntimeError):
    """Carries the nonce and locally computed hash of an uncertain send for reconcile."""

    def __init__(self, message: str, *, nonce: int | None = None, tx_hash: str = "", leg: str = "") -> None:
        super().__init__(message)
        self.nonce, self.tx_hash, self.leg = nonce, tx_hash, leg


class PaperExecution:
    """A deterministic fill adapter. It never imports a key or touches a node."""

    def execute(self, impulse: Impulse) -> tuple[str, int | None]:
        return f"paper-{impulse.id}", None


@dataclass(frozen=True)
class EvmExecutionConfig:
    rpc_url: str
    chain_id: int
    wallet_address: str
    private_key: str
    router_address: str
    quoter_address: str
    weth_address: str
    confirmations: int = 2
    slippage_bps: int = 100
    deadline_sec: int = 45
    gas_limit: int = 350_000
    max_gas_gwei: float = 1.0


ROUTER_ABI: list[dict[str, Any]] = [{
    "name": "exactInputSingle", "outputs": [{"name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable", "type": "function", "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ]}],
}]
QUOTER_ABI: list[dict[str, Any]] = [{
    "name": "quoteExactInputSingle", "outputs": [{"type": "uint256"}, {"type": "uint160"}, {"type": "uint32"}, {"type": "uint256"}],
    "stateMutability": "nonpayable", "type": "function", "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"}, {"name": "amountIn", "type": "uint256"},
        {"name": "fee", "type": "uint24"}, {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ]}],
}]
ERC20_ABI: list[dict[str, Any]] = [{
    "name": "allowance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function",
    "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
}, {"name": "approve", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function",
    "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}]},
    {"name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function",
     "inputs": [{"name": "account", "type": "address"}]}]


class EvmExecution:
    """Uniswap V3 SwapRouter02 adapter for Robinhood Chain/Base-like EVMs.

    The impulse must carry a fresh ``amount_in_wei`` and ``fee`` from a
    read-only quote. This adapter recomputes ``amountOutMinimum``
    immediately before signing and sends each nonce exactly once.
    """

    def __init__(self, config: EvmExecutionConfig) -> None:
        if not config.private_key:
            raise ExecutionError("private key is required only inside the signer process")
        from eth_account import Account

        self.config = config
        self.w3 = Web3(RetryingHTTPProvider(config.rpc_url, request_kwargs={"timeout": 10}))
        if int(self.w3.eth.chain_id) != config.chain_id:
            raise ExecutionError(f"wrong chain: got {self.w3.eth.chain_id}, expected {config.chain_id}")
        self.account = Account.from_key(config.private_key)
        if self.account.address.lower() != config.wallet_address.lower():
            raise ExecutionError("WALLET_ADDRESS does not match PRIVATE_KEY")
        self.router = self.w3.eth.contract(address=Web3.to_checksum_address(config.router_address), abi=ROUTER_ABI)
        self.quoter = self.w3.eth.contract(address=Web3.to_checksum_address(config.quoter_address), abi=QUOTER_ABI)

    def _send_once(self, tx: dict[str, Any], leg: str) -> str:
        from eth_account import Account

        signed = Account.sign_transaction(tx, self.config.private_key)
        local_hash = "0x" + bytes(signed.hash).hex()
        # Never retry this call: a provider timeout does not prove the tx was absent.
        try:
            return self.w3.eth.send_raw_transaction(signed.raw_transaction).hex()
        except Exception as exc:
            raise ExecutionError(f"{leg} send uncertain; reconcile nonce {tx['nonce']}, never resend",
                                 nonce=int(tx["nonce"]), tx_hash=local_hash, leg=leg) from exc

    def wallet_snapshot(self) -> tuple[float, float, float]:
        """Return native ETH, WETH and gas price without mutating chain state."""
        address = Web3.to_checksum_address(self.config.wallet_address)
        weth = self.w3.eth.contract(address=Web3.to_checksum_address(self.config.weth_address), abi=ERC20_ABI)
        native = float(self.w3.from_wei(self.w3.eth.get_balance(address), "ether"))
        wrapped = float(self.w3.from_wei(weth.functions.balanceOf(address).call(), "ether"))
        gas = float(self.w3.from_wei(self.w3.eth.gas_price, "gwei"))
        return native, wrapped, gas

    def execute(self, impulse: Impulse) -> tuple[str, int | None]:
        try:
            amount_in = int(impulse.metadata["amount_in_wei"])
            amount_out, *_ = self.quoter.functions.quoteExactInputSingle((
                Web3.to_checksum_address(self.config.weth_address), Web3.to_checksum_address(impulse.token),
                amount_in, int(impulse.metadata["fee"]), 0,
            )).call()
            quote_out = int(amount_out)
            fee = int(impulse.metadata["fee"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionError("impulse lacks a fresh swap quote") from exc
        amount_min = quote_out * (10_000 - self.config.slippage_bps) // 10_000
        recipient = Web3.to_checksum_address(self.config.wallet_address)
        token_out = Web3.to_checksum_address(impulse.token)
        nonce = int(self.w3.eth.get_transaction_count(recipient, "pending"))
        gas_price = int(self.w3.eth.gas_price)
        if gas_price > self.w3.to_wei(self.config.max_gas_gwei, "gwei"):
            raise ExecutionError("gas cap exceeded; no transaction was signed")
        # WETH is an ERC-20 input. Approve once, wait for inclusion, then use
        # the next pending nonce for the swap. Approval and swap are separate
        # intents in operational logs even though this adapter exposes one call.
        weth = self.w3.eth.contract(address=Web3.to_checksum_address(self.config.weth_address), abi=ERC20_ABI)
        allowance = int(weth.functions.allowance(recipient, self.router.address).call())
        if allowance < amount_in:
            approval = weth.functions.approve(self.router.address, amount_in).build_transaction({  # type: ignore[arg-type]
                "from": recipient, "nonce": nonce, "chainId": self.config.chain_id,
                "gas": 90_000, "maxFeePerGas": gas_price,
                "maxPriorityFeePerGas": min(gas_price, self.w3.to_wei(0.01, "gwei")),
            })
            approval_hash = self._send_once(dict(approval), "approval")
            try:
                approval_receipt = self.w3.eth.wait_for_transaction_receipt(approval_hash, timeout=90)  # type: ignore[arg-type]
            except TimeExhausted as exc:
                raise ExecutionError(f"approval timeout for {approval_hash}; reconcile by nonce",
                                     nonce=nonce, tx_hash=approval_hash, leg="approval") from exc
            if int(approval_receipt["status"]) != 1:
                raise ExecutionError(f"approval reverted: {approval_hash}", nonce=nonce, tx_hash=approval_hash, leg="approval")
            nonce += 1
        params = (Web3.to_checksum_address(self.config.weth_address), token_out, fee, recipient, amount_in, amount_min, 0)
        tx = self.router.functions.exactInputSingle(params).build_transaction({  # type: ignore[arg-type]
            "from": recipient, "value": 0, "nonce": nonce, "chainId": self.config.chain_id,
            "gas": self.config.gas_limit, "maxFeePerGas": gas_price,
            "maxPriorityFeePerGas": min(gas_price, self.w3.to_wei(0.01, "gwei")),
        })
        tx_hash = self._send_once(dict(tx), "swap")
        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)  # type: ignore[arg-type]
        except TimeExhausted as exc:
            raise ExecutionError(f"receipt timeout for {tx_hash}; reconcile by nonce",
                                 nonce=nonce, tx_hash=tx_hash, leg="swap") from exc
        target = int(receipt["blockNumber"]) + self.config.confirmations
        while int(self.w3.eth.block_number) < target:
            time.sleep(0.1)
        if int(receipt["status"]) != 1:
            raise ExecutionError(f"swap reverted: {tx_hash}", nonce=nonce, tx_hash=tx_hash, leg="swap")
        return tx_hash, nonce
