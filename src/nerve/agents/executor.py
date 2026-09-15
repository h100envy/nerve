from __future__ import annotations

from typing import Protocol

from ..models import Impulse, NodeType, Verdict
from ..protocol import NerveNode
from ..store import NerveStore


class ExecutionAdapter(Protocol):
    def execute(self, impulse: Impulse) -> tuple[str, int | None]: ...


class ExecutorNode(NerveNode):
    node_type = NodeType.EXECUTOR
    owns = "creates an idempotent intent and delegates one paper/live execution"
    boundary = "does not change risk size, retry an unknown send or hide a revert"

    def __init__(self, adapter: ExecutionAdapter, store: NerveStore) -> None:
        self.adapter, self.store = adapter, store

    def process(self, impulse: Impulse) -> Impulse:
        if impulse.verdict is Verdict.REJECT:
            return impulse
        client_id = f"nerve-{impulse.id}"
        existing = self.store.get_intent(client_id)
        if existing and existing["status"] in {"submitted", "filled", "reverted", "unknown"}:
            impulse.tx_hash = str(existing.get("tx_hash") or "")
            return impulse.advance(NodeType.EXECUTOR, Verdict.ALERT, "idempotent replay; reconcile existing intent")
        if not self.store.create_intent(client_id, impulse.id, None, {"token": impulse.token, "size_usd": str(impulse.size_usd)}):
            return impulse.advance(NodeType.EXECUTOR, Verdict.ALERT, "intent already exists; reconcile")
        try:
            tx_hash, nonce = self.adapter.execute(impulse)
        except Exception as exc:
            self.store.update_intent(client_id, "unknown", error=str(exc))
            return impulse.advance(NodeType.EXECUTOR, Verdict.ALERT, "send uncertain; reconcile by nonce")
        impulse.tx_hash, impulse.nonce = tx_hash, nonce
        self.store.update_intent(client_id, "filled", tx_hash=tx_hash)
        return impulse.advance(NodeType.EXECUTOR, Verdict.FILL, tx_hash)
