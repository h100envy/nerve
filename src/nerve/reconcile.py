from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from web3 import Web3

from .chainread import ChainReader, calldata, decode_uint, eth_call
from .store import NerveStore


class ReconcileChain(Protocol):
    def nonce(self, wallet: str, tag: str) -> int: ...
    def receipt(self, tx_hash: str) -> dict[str, Any] | None: ...
    def token_balance(self, token: str, wallet: str) -> int: ...


class RpcReconcileChain:
    """Nonce, receipt and balance reads. Built on the read-only reader, so it cannot send."""

    def __init__(self, reader: ChainReader) -> None:
        self.reader = reader

    def nonce(self, wallet: str, tag: str) -> int:
        return int(self.reader.request("eth_getTransactionCount", [Web3.to_checksum_address(wallet), tag]), 16)

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        result = self.reader.request("eth_getTransactionReceipt", [tx_hash])
        return result if isinstance(result, dict) else None

    def token_balance(self, token: str, wallet: str) -> int:
        latest = int(self.reader.request("eth_blockNumber", []), 16)
        data = calldata("balanceOf(address)", ["address"], [Web3.to_checksum_address(wallet)])
        return decode_uint(eth_call(self.reader, Web3.to_checksum_address(token), data, latest))


@dataclass(frozen=True)
class ReconcileRow:
    kind: str
    ref: str
    recorded: str
    observed: str
    action: str
    divergent: bool


def _hash(value: object) -> str:
    text = str(value or "")
    return text if not text or text.startswith("0x") else "0x" + text


def reconcile(store: NerveStore, chain_factory: Callable[[], ReconcileChain], wallet: str) -> list[ReconcileRow]:
    """Resolve `unknown` intents by nonce and check open positions against the wallet.

    Intent statuses are updated in the local store. Positions are never corrected:
    a divergence is reported for a human. No transaction is ever built or sent.
    """
    intents = store.intents_with_status("unknown")
    positions = store.open_positions()
    live_positions = [row for row in positions if not str(row["tx_hash"]).startswith("paper-")]
    rows = [ReconcileRow("position", str(row["token"]), f"open size=${row['size_usd']}", "paper fill",
                         "none: paper positions are not on-chain", False)
            for row in positions if row not in live_positions]
    if not intents and not live_positions:
        return rows
    if not wallet:
        raise ValueError("WALLET_ADDRESS is required to reconcile unknown intents or live positions")
    chain = chain_factory()
    held = {str(row["token"]).lower() for row in positions}
    if intents:
        pending, latest = chain.nonce(wallet, "pending"), chain.nonce(wallet, "latest")
    for intent in intents:
        client_id, nonce, leg = str(intent["client_id"]), intent["nonce"], str(intent["payload"].get("leg", "swap"))
        tx_hash = _hash(intent["tx_hash"])
        recorded = f"unknown nonce={nonce} leg={leg} tx={tx_hash or '-'}"
        if nonce is None:
            rows.append(ReconcileRow("intent", client_id, recorded, "no nonce recorded",
                                     "left unknown: manual review", True))
            continue
        if nonce >= pending:
            store.update_intent(client_id, "never_sent", reconciled=f"nonce {nonce} >= pending {pending}")
            rows.append(ReconcileRow("intent", client_id, recorded, f"pending nonce={pending}", "set never_sent", False))
            continue
        if nonce >= latest:
            rows.append(ReconcileRow("intent", client_id, recorded, f"in mempool (latest={latest} pending={pending})",
                                     "left unknown: wait for inclusion", True))
            continue
        receipt = chain.receipt(tx_hash) if tx_hash else None
        if receipt is None:
            rows.append(ReconcileRow("intent", client_id, recorded, f"nonce consumed (latest={latest}); recorded tx not found",
                                     "left unknown: nonce used by another tx", True))
            continue
        succeeded = int(str(receipt.get("status", "0x0")), 16) == 1
        observed = f"receipt status={'1' if succeeded else '0'} block={int(str(receipt.get('blockNumber', '0x0')), 16)}"
        if leg == "approval":
            # The swap is only sent after a confirmed approval, so it never went out.
            store.update_intent(client_id, "never_sent", reconciled=f"approval {'mined' if succeeded else 'reverted'}")
            rows.append(ReconcileRow("intent", client_id, recorded, observed, "set never_sent (approval leg only)", False))
            continue
        status = "filled" if succeeded else "reverted"
        store.update_intent(client_id, status, reconciled=observed)
        token = str(intent["payload"].get("token", "")).lower()
        missing = succeeded and token not in held
        rows.append(ReconcileRow("intent", client_id, recorded, observed,
                                 f"set {status}" + ("; POSITION MISSING FROM STORE" if missing else ""), missing))
    for position in live_positions:
        balance = chain.token_balance(str(position["token"]), wallet)
        backed = balance > 0
        rows.append(ReconcileRow("position", str(position["token"]), f"open size=${position['size_usd']}",
                                 f"wallet balance={balance}", "ok" if backed else "NOT BACKED: store says open, wallet holds 0",
                                 not backed))
    return rows


def render(rows: list[ReconcileRow]) -> str:
    header = ("kind", "ref", "recorded", "observed", "action")
    table = [header, *((row.kind, row.ref, row.recorded, row.observed, row.action) for row in rows)]
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip() for line in table]
    lines.insert(1, "  ".join("-" * width for width in widths))
    if not rows:
        lines.append("(nothing to reconcile: no unknown intents, no open positions)")
    divergent = sum(row.divergent for row in rows)
    lines.append(f"divergences={divergent}")
    return "\n".join(lines)


def report_divergences(rows: list[ReconcileRow]) -> None:
    for row in rows:
        if row.divergent:
            print(f"DIVERGENCE {row.kind} {row.ref}: {row.observed} -> {row.action}", file=sys.stderr)
