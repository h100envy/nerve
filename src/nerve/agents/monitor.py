from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ..models import Impulse, NodeType, Verdict
from ..protocol import NerveNode


class MonitorNode(NerveNode):
    node_type = NodeType.MONITOR
    owns = "watches positions, receipts, stops and liquidity drains"
    boundary = "does not reopen rejected entries or claim a stop exists on-chain"

    def __init__(self, kill_switch_file: Path, stop_pct: Decimal = Decimal("0.20")) -> None:
        self.kill_switch_file, self.stop_pct = kill_switch_file, stop_pct

    def process(self, impulse: Impulse) -> Impulse:
        pnl = Decimal(str(impulse.metadata.get("pnl_pct", "0")))
        if pnl <= -self.stop_pct:
            return impulse.advance(NodeType.MONITOR, Verdict.STOP, f"stop threshold reached pnl={pnl}")
        if impulse.metadata.get("liquidity_drained"):
            return impulse.advance(NodeType.MONITOR, Verdict.STOP, "liquidity drain")
        return impulse.advance(NodeType.MONITOR, Verdict.PASS, "position healthy")
