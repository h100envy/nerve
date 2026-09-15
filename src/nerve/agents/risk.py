from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ..models import Impulse, NodeType, PortfolioContext, Verdict
from ..protocol import NerveNode


class RiskNode(NerveNode):
    node_type = NodeType.RISK
    owns = "computes deterministic limits, size and stop geometry"
    boundary = "does not ask GPT for permission and never signs or sends a transaction"

    def __init__(self, kill_switch_file: Path, risk_per_trade_pct: Decimal = Decimal("0.01"), max_position_pct: Decimal = Decimal("0.05"), min_rr: Decimal = Decimal("1.5")) -> None:
        self.kill_switch_file = kill_switch_file
        self.risk_per_trade_pct, self.max_position_pct, self.min_rr = risk_per_trade_pct, max_position_pct, min_rr

    def process(self, impulse: Impulse) -> Impulse:
        # Context is injected by Spine before this node in metadata. Keeping
        # the node pure makes it easy to run in a worker or replay a decision.
        context = PortfolioContext.model_validate(impulse.metadata.get("portfolio", {}))
        if self.kill_switch_file.exists():
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "kill switch is active")
        if context.daily_pnl_pct <= -context.daily_loss_limit_pct:
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "daily loss limit breached")
        if context.open_positions >= context.max_positions:
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "maximum positions reached")
        if impulse.token.lower() in {token.lower() for token in context.held_tokens}:
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "already holding token")
        if impulse.confidence <= 0:
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "analyst confidence is missing")
        stop_distance = max(impulse.slippage_bps / Decimal("10000") * 3, Decimal("0.02"))
        risk_budget = context.equity_usd * self.risk_per_trade_pct
        notional_cap = context.equity_usd * self.max_position_pct
        size = min(risk_budget / stop_distance, notional_cap)
        if size <= 0:
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "non-positive calculated size")
        impulse.size_usd = size.quantize(Decimal("0.01"))
        entry = Decimal(str(impulse.metadata.get("entry_price_usd", "0")))
        if entry > 0:
            impulse.entry_price = entry
            impulse.stop_price = (entry * (Decimal("1") - stop_distance)).quantize(Decimal("0.00000001"))
            impulse.take_profit_price = (entry * (Decimal("1") + stop_distance * self.min_rr)).quantize(Decimal("0.00000001"))
        weth_usd = Decimal(str(impulse.metadata.get("weth_usd", "0")))
        if weth_usd <= 0:
            return impulse.advance(NodeType.RISK, Verdict.REJECT, "WETH/USD reference is unavailable")
        impulse.metadata["amount_in_wei"] = int((impulse.size_usd / weth_usd) * Decimal(10**18))
        impulse.metadata["risk_budget_usd"] = str(risk_budget)
        impulse.metadata["stop_distance_pct"] = str(stop_distance)
        return impulse.advance(NodeType.RISK, Verdict.EXECUTE, f"size=${impulse.size_usd} rr_floor={self.min_rr}")
