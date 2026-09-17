from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .models import Impulse, NodeType, PortfolioContext, Verdict


@dataclass(frozen=True)
class Reflex:
    name: str
    check: Callable[[Impulse, PortfolioContext], bool]
    reject_note: str
    # Nodes this reflex guards. Empty means every node after SCANNER.
    guards: frozenset[NodeType] = frozenset()

    def fire(self, impulse: Impulse, context: PortfolioContext) -> Impulse | None:
        if self.check(impulse, context):
            return impulse.advance(NodeType.RISK, Verdict.REJECT, f"reflex:{self.name} {self.reject_note}")
        return None


# SENTINEL facts exist only after SENTINEL has run, so these reflexes guard the
# nodes behind it. A route without SENTINEL still trips them: fail closed.
AFTER_SENTINEL = frozenset({NodeType.ANALYST, NodeType.RISK, NodeType.EXECUTOR})


def sentinel_report(impulse: Impulse) -> dict[str, Any]:
    report = impulse.metadata.get("sentinel")
    return report if isinstance(report, dict) else {}


def simulation_is_stale(impulse: Impulse, context: PortfolioContext) -> bool:
    pinned = sentinel_report(impulse).get("block_number")
    if not isinstance(pinned, int) or isinstance(pinned, bool) or context.head_block is None:
        return True
    return context.head_block - pinned > context.sentinel_max_age_blocks


def default_reflexes() -> tuple[Reflex, ...]:
    return (
        Reflex("kill_switch", lambda _i, c: c.kill_switch_active, "kill switch is active"),
        Reflex("daily_loss", lambda _i, c: c.daily_pnl_pct <= -c.daily_loss_limit_pct, "daily loss limit breached"),
        Reflex("gas_cap", lambda _i, c: c.gas_gwei > c.max_gas_gwei, "gas above cap"),
        Reflex("sell_simulation_failed", lambda i, _c: i.sell_route is False or sentinel_report(i).get("sell_leg") != "ok",
               "sell leg reverted, was not simulated, or sell_route is false", AFTER_SENTINEL),
        Reflex("buy_tax_above_cap", lambda i, c: i.buy_tax_pct is None or i.buy_tax_pct > c.max_buy_tax_pct,
               "buy tax unmeasured or above cap", AFTER_SENTINEL),
        Reflex("sell_tax_above_cap", lambda i, c: i.sell_tax_pct is None or i.sell_tax_pct > c.max_sell_tax_pct,
               "sell tax unmeasured or above cap", AFTER_SENTINEL),
        Reflex("stale_simulation", simulation_is_stale, "simulation block missing or older than SENTINEL_MAX_AGE_BLOCKS",
               AFTER_SENTINEL),
        Reflex("low_liquidity", lambda i, c: i.liquidity_usd < c.min_liquidity_usd, "liquidity below minimum"),
        Reflex("high_slippage", lambda i, c: i.slippage_bps > c.max_slippage_bps, "slippage above limit"),
        Reflex("low_score", lambda i, c: i.score < c.min_score, "score below minimum"),
        Reflex("duplicate", lambda i, c: i.token.lower() in {token.lower() for token in c.held_tokens}, "already holding token"),
        Reflex("max_positions", lambda _i, c: c.open_positions >= c.max_positions, "maximum positions reached"),
    )


def check_reflexes(impulse: Impulse, context: PortfolioContext, reflexes: tuple[Reflex, ...],
                   before: NodeType | None = None) -> Impulse:
    for reflex in reflexes:
        if before is not None and reflex.guards and before not in reflex.guards:
            continue
        result = reflex.fire(impulse, context)
        if result is not None:
            return result
    return impulse
