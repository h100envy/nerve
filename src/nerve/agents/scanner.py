from __future__ import annotations

from collections.abc import Iterable

from ..models import Impulse, NodeType, Verdict
from ..protocol import NerveNode
from ..score import nerve_score
from ..sources import PoolSource


class ScannerNode(NerveNode):
    node_type = NodeType.SCANNER
    owns = "normalizes pool observations and applies cheap deterministic filters"
    boundary = "does not call GPT, size positions, sign or send transactions"

    def __init__(self, source: PoolSource) -> None:
        self.source = source

    def discover(self) -> Iterable[Impulse]:
        return self.source.discover()

    def process(self, impulse: Impulse) -> Impulse:
        if not impulse.token or not impulse.pool:
            return impulse.advance(NodeType.SCANNER, Verdict.REJECT, "token and pool are required")
        impulse.score = nerve_score(impulse)
        if not impulse.buy_route or not impulse.sell_route:
            return impulse.advance(NodeType.SCANNER, Verdict.REJECT, "missing buy or sell route")
        if impulse.liquidity_usd <= 0:
            return impulse.advance(NodeType.SCANNER, Verdict.REJECT, "no liquidity reported")
        return impulse.advance(NodeType.SCANNER, Verdict.PASS, f"score={impulse.score}")
