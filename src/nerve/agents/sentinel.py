from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any

import httpx
from web3 import Web3

from ..chainread import (
    DEAD_ADDRESS,
    MAX_UINT256,
    ZERO_ADDRESS,
    ChainReader,
    RpcError,
    address_topic,
    calldata,
    decode_address,
    decode_uint,
    eth_call,
    get_logs,
    selector,
    to_bytes,
    topic_address,
)
from ..models import Impulse, NodeType, Verdict
from ..protocol import NerveNode
from ..score import nerve_score
from ..sources import load_enrichment

QUOTE_SIG = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"
QUOTE_TYPES = "(address,address,uint256,uint24,uint160)"
SWAP_SIG = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
SWAP_TYPES = "(address,address,uint24,address,uint256,uint256,uint160)"

TRANSFER_TOPIC = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
POOL_CREATED_TOPIC = "0x" + bytes(Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)")).hex()
MINT_TOPIC = "0x" + bytes(Web3.keccak(text="Mint(address,address,int24,int24,uint128,uint256,uint256)")).hex()
INCREASE_LIQUIDITY_TOPIC = "0x" + bytes(Web3.keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)")).hex()
# EIP-1967 implementation slot: keccak256("eip1967.proxy.implementation") - 1.
EIP1967_IMPLEMENTATION_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

# Native ETH granted to the wallet inside the simulation only, on top of the
# simulated size. It is wrapped to WETH by the first simulated call.
FUNDING_HEADROOM_WEI = 10**18
PUSH4 = b"\x63"
LP_LOCKED_MIN_SHARE = Decimal("0.95")
HOLDER_MIN_COVERAGE = Decimal("0.90")
BALANCE_BATCH = 100

# Presence is a flag, not a reject: many legitimate tokens have an owner.
HOSTILE_SELECTORS: dict[str, tuple[str, ...]] = {
    "blacklist": ("setBlacklist(address,bool)", "blacklist(address)", "blacklist(address,bool)",
                  "addToBlacklist(address)", "setBlacklisted(address,bool)", "setBots(address[],bool)",
                  "blockBots(address[])"),
    "max_wallet": ("setMaxWallet(uint256)", "setMaxWalletSize(uint256)", "setMaxWalletAmount(uint256)",
                   "setMaxTxAmount(uint256)", "setMaxTransactionAmount(uint256)"),
    "pause": ("pause()", "unpause()", "setPaused(bool)"),
    "trading_toggle": ("setTradingEnabled(bool)", "enableTrading()", "openTrading()", "setTradingOpen(bool)"),
    "mint": ("mint(address,uint256)", "mint(uint256)"),
    "tax_setter": ("setTaxes(uint256,uint256)", "setFees(uint256,uint256)", "setTax(uint256)",
                   "setBuyTax(uint256)", "setSellTax(uint256)", "updateFees(uint256,uint256)"),
    "fee_exclusion": ("excludeFromFee(address)", "excludeFromFees(address,bool)",
                      "setExcludedFromFee(address,bool)", "includeInFee(address)"),
    "whitelist_gate": ("setWhitelist(address,bool)", "addToWhitelist(address)", "setWhitelistEnabled(bool)",
                       "setOnlyWhitelisted(bool)"),
    "transfer_hook": ("setTransferHook(address)", "setAntiBot(address)", "setAntiBotEnabled(bool)",
                      "setCooldownEnabled(bool)", "setTransferDelayEnabled(bool)", "setGuard(address)"),
}
_HOSTILE_PATTERNS = {name: tuple(PUSH4 + selector(sig) for sig in sigs) for name, sigs in HOSTILE_SELECTORS.items()}


@dataclass(frozen=True)
class SentinelSettings:
    wallet_address: str
    weth_address: str
    router_address: str
    quoter_address: str
    factory_address: str
    weth_usd: Decimal
    simulate_size_usd: Decimal = Decimal("500")
    max_age_blocks: int = 30
    position_manager_address: str = ""
    lp_locker_allowlist: tuple[str, ...] = ()
    indexer_url: str = ""
    enrichment_path: Path | None = None
    history_window_blocks: int = 36_000
    log_chunk_blocks: int = 5_000
    max_holder_candidates: int = 400


@dataclass(frozen=True)
class SimCall:
    ok: bool
    data: bytes
    error: str = ""


@dataclass
class RoundTrip:
    buy_leg: str = "unavailable"
    sell_leg: str = "unavailable"
    buy_tax_pct: Decimal | None = None
    sell_tax_pct: Decimal | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def tax_pct(quoted: int | None, received: int) -> Decimal | None:
    """Share of the quoted amount that never arrived. Rounded up: never flatter a token."""
    if not quoted:
        return None
    if received >= quoted:
        return Decimal("0")
    return (Decimal(quoted - received) * 100 / Decimal(quoted)).quantize(Decimal("0.01"), rounding=ROUND_UP)


def revert_reason(call: SimCall) -> str:
    if call.data[:4] == selector("Error(string)") and len(call.data) >= 68:
        try:
            length = int.from_bytes(call.data[36:68], "big")
            return call.data[68:68 + length].decode(errors="replace")[:200]
        except ValueError:
            pass
    return call.error[:200] or "reverted"


class SentinelNode(NerveNode):
    node_type = NodeType.SENTINEL
    owns = "simulates a round trip from the real wallet and measures transfer tax"
    boundary = "never signs, never broadcasts, never asks the model, never guesses a missing value"

    def __init__(self, reader: ChainReader, settings: SentinelSettings) -> None:
        self.reader, self.settings = reader, settings
        self.lockers = {address.lower() for address in settings.lp_locker_allowlist} | {ZERO_ADDRESS, DEAD_ADDRESS}

    def head_block(self) -> int | None:
        try:
            return int(self.reader.request("eth_blockNumber", []), 16)
        except Exception:
            return None

    def process(self, impulse: Impulse) -> Impulse:
        if impulse.verdict is Verdict.REJECT:
            return impulse
        # Unmeasured means unknown. Never carry a tax value from an upstream source.
        impulse.buy_tax_pct = impulse.sell_tax_pct = None
        block = self.head_block()
        if block is None:
            impulse.metadata["sentinel"] = {"buy_leg": "unavailable", "sell_leg": "unavailable", "error": "no block number"}
            return impulse.advance(NodeType.SENTINEL, Verdict.REJECT, "sell_simulation unavailable: no block number")
        report: dict[str, Any] = {"block_number": block, "max_age_blocks": self.settings.max_age_blocks, "sources": {}}
        impulse.metadata["sentinel"] = report

        try:
            trip = self._round_trip(impulse, block)
        except Exception as exc:
            trip = RoundTrip(detail={"error": f"simulation failed: {exc}"[:300]})
        report.update({"buy_leg": trip.buy_leg, "sell_leg": trip.sell_leg, **trip.detail})
        impulse.buy_tax_pct, impulse.sell_tax_pct = trip.buy_tax_pct, trip.sell_tax_pct
        if trip.buy_leg in {"reverted", "empty"}:
            impulse.buy_route = impulse.sell_route = False
        elif trip.sell_leg in {"reverted", "empty"}:
            impulse.buy_route, impulse.sell_route = True, False
        elif trip.buy_leg == trip.sell_leg == "ok":
            impulse.buy_route = impulse.sell_route = True

        flags = self._guard(lambda: self._scan_bytecode(impulse.token, block, report), [], report, "bytecode")
        impulse.risk_flags = list(dict.fromkeys([*impulse.risk_flags, *flags]))

        if trip.buy_leg == trip.sell_leg == "ok":
            self._enrich(impulse, block, report)
        self._stamp_freshness(report, block)
        impulse.score = nerve_score(impulse)

        if trip.buy_leg != "ok":
            reason = trip.detail.get("error", trip.buy_leg)
            return impulse.advance(NodeType.SENTINEL, Verdict.REJECT, f"buy_simulation {trip.buy_leg}; sell_simulation not run: {reason}")
        if trip.sell_leg != "ok":
            reason = trip.detail.get("error", trip.sell_leg)
            return impulse.advance(NodeType.SENTINEL, Verdict.REJECT, f"sell_simulation {trip.sell_leg}: {reason}")
        return impulse.advance(NodeType.SENTINEL, Verdict.PASS,
                               f"block={block} buy_tax={impulse.buy_tax_pct}% sell_tax={impulse.sell_tax_pct}% "
                               f"flags={len(flags)} score={impulse.score}")

    # -- round trip ---------------------------------------------------------

    def _round_trip(self, impulse: Impulse, block: int) -> RoundTrip:
        s = self.settings
        fee = impulse.metadata.get("fee")
        if not s.wallet_address:
            return RoundTrip(detail={"error": "WALLET_ADDRESS is not configured"})
        if s.weth_usd <= 0:
            return RoundTrip(detail={"error": "WETH_USD reference is unavailable"})
        if fee is None or not impulse.token:
            return RoundTrip(detail={"error": "pool fee tier or token is unknown"})
        wallet, weth = Web3.to_checksum_address(s.wallet_address), Web3.to_checksum_address(s.weth_address)
        token, router = Web3.to_checksum_address(impulse.token), Web3.to_checksum_address(s.router_address)
        quoter, fee = Web3.to_checksum_address(s.quoter_address), int(fee)
        amount_in = int(s.simulate_size_usd / s.weth_usd * Decimal(10**18))
        detail: dict[str, Any] = {"wallet": wallet, "size_usd": str(s.simulate_size_usd), "amount_in_wei": str(amount_in)}

        # State override funds native ETH only; WETH and the router allowance
        # are then created by the wallet's own calls inside the simulated block,
        # so no token storage layout is assumed.
        overrides: dict[str, Any] = {wallet: {"balance": hex(amount_in + FUNDING_HEADROOM_WEI)}}
        buy_calls = [
            self._call(wallet, weth, calldata("deposit()"), value=amount_in),
            self._call(wallet, weth, calldata("approve(address,uint256)", ["address", "uint256"], [router, MAX_UINT256])),
            self._call(wallet, quoter, calldata(QUOTE_SIG, [QUOTE_TYPES], [(weth, token, amount_in, fee, 0)])),
            self._call(wallet, token, calldata("balanceOf(address)", ["address"], [wallet])),
            self._call(wallet, router, calldata(SWAP_SIG, [SWAP_TYPES], [(weth, token, fee, wallet, amount_in, 0, 0)])),
            self._call(wallet, token, calldata("balanceOf(address)", ["address"], [wallet])),
        ]
        first = self._simulate([buy_calls], overrides, block)
        if not (first[0].ok and first[1].ok):
            return RoundTrip(detail={**detail, "error": "could not wrap and approve WETH in simulation"})
        if not first[4].ok:
            return RoundTrip("reverted", "not_run", detail={**detail, "error": revert_reason(first[4])})
        delivered = decode_uint(first[5].data) - decode_uint(first[3].data)
        buy_quote = decode_uint(first[2].data) if first[2].ok else None
        detail.update({"buy_quote": str(buy_quote), "buy_delivered": str(delivered)})
        if delivered <= 0:
            return RoundTrip("empty", "not_run", detail={**detail, "error": "buy delivered zero tokens"})

        sell_calls = [
            self._call(wallet, token, calldata("approve(address,uint256)", ["address", "uint256"], [router, MAX_UINT256])),
            self._call(wallet, quoter, calldata(QUOTE_SIG, [QUOTE_TYPES], [(token, weth, delivered, fee, 0)])),
            self._call(wallet, weth, calldata("balanceOf(address)", ["address"], [wallet])),
            self._call(wallet, router, calldata(SWAP_SIG, [SWAP_TYPES], [(token, weth, fee, wallet, delivered, 0, 0)])),
            self._call(wallet, weth, calldata("balanceOf(address)", ["address"], [wallet])),
        ]
        # The sell lands in the next simulated block, as a real exit would, so a
        # one-transaction-per-block anti-bot rule is not mistaken for a honeypot.
        second = self._simulate([buy_calls, sell_calls], overrides, block)
        replayed = second[4].ok and decode_uint(second[5].data) - decode_uint(second[3].data) == delivered
        if not replayed:
            return RoundTrip(detail={**detail, "error": "buy leg did not replay identically at the pinned block"})
        buy_tax = tax_pct(buy_quote, delivered)
        if not second[6].ok:
            return RoundTrip("ok", "reverted", buy_tax, detail={**detail, "error": "approve: " + revert_reason(second[6])})
        if not second[9].ok:
            return RoundTrip("ok", "reverted", buy_tax, detail={**detail, "error": revert_reason(second[9])})
        weth_out = decode_uint(second[10].data) - decode_uint(second[8].data)
        sell_quote = decode_uint(second[7].data) if second[7].ok else None
        detail.update({"sell_quote": str(sell_quote), "sell_received": str(weth_out)})
        if weth_out <= 0:
            return RoundTrip("ok", "empty", buy_tax, detail={**detail, "error": "sell returned zero WETH"})
        return RoundTrip("ok", "ok", buy_tax, tax_pct(sell_quote, weth_out), detail=detail)

    @staticmethod
    def _call(sender: str, to: str, data: str, value: int = 0) -> dict[str, str]:
        call = {"from": sender, "to": to, "data": data}
        if value:
            call["value"] = hex(value)
        return call

    def _simulate(self, blocks: list[list[dict[str, str]]], overrides: dict[str, Any], block: int) -> list[SimCall]:
        """Run consecutive simulated blocks on top of the pinned block; return every call in order."""
        states: list[dict[str, Any]] = [{"calls": calls} for calls in blocks]
        states[0]["stateOverrides"] = overrides
        payload = {"blockStateCalls": states, "validation": False}
        result = self.reader.request("eth_simulateV1", [payload, hex(block)]) or []
        raw = [call for simulated in result for call in simulated.get("calls", [])]
        if len(raw) != sum(len(calls) for calls in blocks):
            raise RpcError("eth_simulateV1 returned an unexpected call count")
        return [SimCall(ok=int(str(item.get("status", "0x0")), 16) == 1, data=to_bytes(item.get("returnData")),
                        error=str((item.get("error") or {}).get("message", ""))) for item in raw]

    # -- bytecode -----------------------------------------------------------

    def _scan_bytecode(self, token: str, block: int, report: dict[str, Any]) -> list[str]:
        code = to_bytes(self.reader.request("eth_getCode", [token, hex(block)]))
        report["code_bytes"] = len(code)
        flags: list[str] = []
        slot = to_bytes(self.reader.request("eth_getStorageAt", [token, EIP1967_IMPLEMENTATION_SLOT, hex(block)]))
        implementation = "0x" + slot[-20:].hex() if slot and any(slot) else ""
        if implementation:
            report["proxy_implementation"] = implementation
            flags.append("bytecode:upgradeable_proxy")
            code += to_bytes(self.reader.request("eth_getCode", [implementation, hex(block)]))
        if not code:
            report["bytecode_error"] = "no code at pinned block"
            return flags
        for name, patterns in _HOSTILE_PATTERNS.items():
            if any(pattern in code for pattern in patterns):
                flags.append(f"bytecode:{name}")
        return flags

    # -- enrichment ---------------------------------------------------------

    def _enrich(self, impulse: Impulse, block: int, report: dict[str, Any]) -> None:
        sources: dict[str, str] = report["sources"]
        fallback: dict[str, Any] | None = None

        def from_file(key: str) -> Any:
            nonlocal fallback
            if fallback is None:
                loaded = self._guard(lambda: load_enrichment(self.settings.enrichment_path), {}, report, "enrichment_file")
                fallback = loaded.get(impulse.token.lower(), {})
            return fallback.get(key)

        renounced = self._guard(lambda: self._mint_renounced(impulse.token, block, report), None, report, "owner")
        if renounced is None and from_file("mint_renounced") is not None:
            renounced, sources["mint_renounced"] = bool(from_file("mint_renounced")), "enrichment_file"
        else:
            sources["mint_renounced"] = "owner_call" if renounced is not None else "unavailable"
        impulse.mint_renounced = renounced

        locked = self._guard(lambda: self._lp_locked(impulse, block, report), None, report, "lp")
        if locked is None and from_file("lp_locked") is not None:
            locked, sources["lp_locked"] = bool(from_file("lp_locked")), "enrichment_file"
        else:
            sources["lp_locked"] = "lp_positions" if locked is not None else "unavailable"
        impulse.lp_locked = locked

        top10, source = self._guard(lambda: self._top10(impulse, block, report), (None, "unavailable"), report, "top10")
        if top10 is None and from_file("top10_pct") is not None:
            top10, source = Decimal(str(from_file("top10_pct"))), "enrichment_file"
        sources["top10_pct"] = source
        # top10_pct is not nullable on the Impulse. An underived value keeps the
        # scanner's pessimistic 100 rather than an optimistic guess.
        if top10 is not None:
            impulse.top10_pct = top10

    def _mint_renounced(self, token: str, block: int, report: dict[str, Any]) -> bool | None:
        for signature in ("owner()", "getOwner()"):
            try:
                data = eth_call(self.reader, token, calldata(signature), block)
            except RpcError:
                continue
            if len(data) >= 32:
                owner = decode_address(data)
                report["owner"] = owner
                return owner in {ZERO_ADDRESS, DEAD_ADDRESS}
        return None

    def _lp_locked(self, impulse: Impulse, block: int, report: dict[str, Any]) -> bool | None:
        s = self.settings
        fee = int(impulse.metadata["fee"])
        token0, token1 = sorted([s.weth_address.lower(), impulse.token.lower()], key=lambda a: int(a, 16))
        window_start = max(0, block - s.history_window_blocks)
        created = get_logs(self.reader, s.factory_address, [POOL_CREATED_TOPIC, address_topic(token0), address_topic(token1),
                                                            "0x" + fee.to_bytes(32, "big").hex()],
                           window_start, block, s.log_chunk_blocks)
        if not created:
            report["lp_error"] = "pool creation is outside the bounded window; earlier positions are invisible"
            return None
        creation_block = int(created[0]["blockNumber"], 16)
        pool = impulse.pool
        manager = s.position_manager_address.lower()
        holdings: dict[str, tuple[str, int]] = {}
        manager_txs: set[str] = set()
        for log in get_logs(self.reader, pool, [MINT_TOPIC], creation_block, block, s.log_chunk_blocks):
            owner = topic_address(log["topics"][1])
            if manager and owner == manager:
                manager_txs.add(str(log["transactionHash"]).lower())
                continue
            if not manager and owner not in self.lockers:
                report["lp_error"] = "UNISWAP_V3_POSITION_MANAGER is not configured; NFT position holders are unresolvable"
                return None
            lower = int.from_bytes(to_bytes(log["topics"][2]), "big", signed=True)
            upper = int.from_bytes(to_bytes(log["topics"][3]), "big", signed=True)
            key = bytes(Web3.keccak(bytes.fromhex(owner[2:]) + lower.to_bytes(3, "big", signed=True)
                                    + upper.to_bytes(3, "big", signed=True)))
            data = eth_call(self.reader, pool, calldata("positions(bytes32)", ["bytes32"], [key]), block)
            holdings[key.hex()] = (owner, decode_uint(data))
        if manager_txs:
            increases = get_logs(self.reader, manager, [INCREASE_LIQUIDITY_TOPIC], creation_block, block, s.log_chunk_blocks)
            token_ids = dict.fromkeys(int(log["topics"][1], 16) for log in increases
                                      if str(log["transactionHash"]).lower() in manager_txs)
            for token_id in token_ids:
                position = eth_call(self.reader, manager, calldata("positions(uint256)", ["uint256"], [token_id]), block)
                words = [position[i:i + 32] for i in range(0, len(position), 32)]
                if len(words) < 8 or {decode_address(words[2]), decode_address(words[3])} != {token0, token1} \
                        or decode_uint(words[4]) != fee:
                    continue
                try:
                    holder = decode_address(eth_call(self.reader, manager, calldata("ownerOf(uint256)", ["uint256"], [token_id]), block))
                except RpcError:
                    continue  # burned NFT holds no liquidity
                holdings[f"nft:{token_id}"] = (holder, decode_uint(words[7]))
        total = sum(liquidity for _holder, liquidity in holdings.values())
        if total == 0:
            report["lp_error"] = "no live liquidity positions found"
            return None
        locked = sum(liquidity for holder, liquidity in holdings.values() if holder in self.lockers)
        share = Decimal(locked) / Decimal(total)
        report["lp_locked_share"] = str(share.quantize(Decimal("0.0001")))
        return share >= LP_LOCKED_MIN_SHARE

    def _top10(self, impulse: Impulse, block: int, report: dict[str, Any]) -> tuple[Decimal | None, str]:
        s = self.settings
        if s.indexer_url:
            try:
                response = httpx.get(s.indexer_url, timeout=10,
                                     params={"chain": str(impulse.chain), "token": impulse.token, "block": block})
                response.raise_for_status()
                value = Decimal(str(response.json()["top10_pct"]))
                if Decimal("0") <= value <= Decimal("100"):
                    return value, "indexer"
            except Exception as exc:
                report["indexer_error"] = str(exc)[:200]
        logs = get_logs(self.reader, impulse.token, [TRANSFER_TOPIC], max(0, block - s.history_window_blocks), block,
                        s.log_chunk_blocks)
        pool = impulse.pool.lower()
        excluded = {pool, ZERO_ADDRESS, DEAD_ADDRESS}
        seen: dict[str, None] = {}
        for log in logs:
            for topic in log.get("topics", [])[1:3]:
                seen.setdefault(topic_address(topic))
        holders = [address for address in seen if address not in excluded][:s.max_holder_candidates]
        if not holders:
            report["top10_error"] = "no transfers in the bounded window"
            return None, "unavailable"
        supply = decode_uint(eth_call(self.reader, impulse.token, calldata("totalSupply()"), block))
        balances = self._balances(impulse.token, [*holders, pool, ZERO_ADDRESS, DEAD_ADDRESS], block)
        circulating = supply - balances[ZERO_ADDRESS] - balances[DEAD_ADDRESS]
        if circulating <= 0:
            report["top10_error"] = "no circulating supply"
            return None, "unavailable"
        held = sorted((balances[address] for address in holders), reverse=True)
        covered = sum(held) + balances[pool]
        coverage = Decimal(covered) / Decimal(circulating)
        report["holder_coverage"] = str(coverage.quantize(Decimal("0.0001")))
        if coverage < HOLDER_MIN_COVERAGE:
            report["top10_error"] = "bounded window does not account for enough supply"
            return None, "unavailable"
        # Any supply not seen in the window is assumed to sit with one top holder:
        # an upper bound, so a short window can only make the number worse.
        bound = Decimal(sum(held[:10]) + max(0, circulating - covered)) * 100 / Decimal(circulating)
        return min(Decimal("100"), bound.quantize(Decimal("0.01"), rounding=ROUND_UP)), "transfer_logs"

    def _balances(self, token: str, addresses: list[str], block: int) -> dict[str, int]:
        result: dict[str, int] = {}
        for start in range(0, len(addresses), BALANCE_BATCH):
            batch = addresses[start:start + BALANCE_BATCH]
            calls = [self._call(ZERO_ADDRESS, token, calldata("balanceOf(address)", ["address"], [Web3.to_checksum_address(a)]))
                     for a in batch]
            for address, call in zip(batch, self._simulate([calls], {}, block), strict=True):
                if not call.ok:
                    raise RpcError(f"balanceOf({address}) reverted")
                result[address] = decode_uint(call.data)
        return result

    # -- freshness ----------------------------------------------------------

    def _stamp_freshness(self, report: dict[str, Any], block: int) -> None:
        head = self.head_block()
        report["observed_at"] = datetime.now(UTC).isoformat()
        report["head_at_observation"] = head
        report["age_blocks"] = None if head is None else max(0, head - block)

    @staticmethod
    def _guard(fn: Any, default: Any, report: dict[str, Any], key: str) -> Any:
        """A derivation that errors is unavailable, never optimistic."""
        try:
            return fn()
        except Exception as exc:
            report[f"{key}_error"] = str(exc)[:200]
            return default
