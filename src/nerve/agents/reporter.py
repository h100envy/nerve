from __future__ import annotations

from ..models import Impulse, NodeType, Verdict
from ..protocol import NerveNode
from ..store import NerveStore


class ReporterNode(NerveNode):
    node_type = NodeType.REPORTER
    owns = "turns the audited funnel into an operational brief"
    boundary = "does not alter positions, verdicts or transaction state"

    def __init__(self, store: NerveStore) -> None:
        self.store = store

    def process(self, impulse: Impulse) -> Impulse:
        return impulse.advance(NodeType.REPORTER, Verdict.PASS, str(self.store.funnel()))

    def brief(self) -> str:
        funnel = self.store.funnel()
        return "NERVE 24h funnel: " + ", ".join(f"{key}={value}" for key, value in sorted(funnel.items()))
