from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import Impulse, NodeType, PortfolioContext, Verdict


@dataclass(frozen=True)
class Reflex:
    name: str
    check: Callable[[Impulse, PortfolioContext], bool]
    reject_note: str

    def fire(self, impulse: Impulse, context: PortfolioContext) -> Impulse | None:
        if self.check(impulse, context):
            return impulse.advance(NodeType.RISK, Verdict.REJECT, f"reflex:{self.name} {self.reject_note}")
        return None


def default_reflexes() -> tuple[Reflex, ...]:
    return (
        Reflex("kill_switch", lambda _i, c: c.kill_switch_active, "kill switch is active"),
        Reflex("daily_loss", lambda _i, c: c.daily_pnl_pct <= -c.daily_loss_limit_pct, "daily loss limit breached"),
        Reflex("gas_cap", lambda _i, c: c.gas_gwei > c.max_gas_gwei, "gas above cap"),
        Reflex("low_liquidity", lambda i, c: i.liquidity_usd < c.min_liquidity_usd, "liquidity below minimum"),
        Reflex("high_slippage", lambda i, c: i.slippage_bps > c.max_slippage_bps, "slippage above limit"),
        Reflex("low_score", lambda i, c: i.score < c.min_score, "score below minimum"),
        Reflex("duplicate", lambda i, c: i.token.lower() in {token.lower() for token in c.held_tokens}, "already holding token"),
        Reflex("max_positions", lambda _i, c: c.open_positions >= c.max_positions, "maximum positions reached"),
    )


def check_reflexes(impulse: Impulse, context: PortfolioContext, reflexes: tuple[Reflex, ...]) -> Impulse:
    for reflex in reflexes:
        result = reflex.fire(impulse, context)
        if result is not None:
            return result
    return impulse
