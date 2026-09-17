from __future__ import annotations

from decimal import Decimal

from .models import Impulse


def nerve_score(impulse: Impulse) -> int:
    """Deterministic 0–100 pool score. Same facts always produce same score."""
    if impulse.sell_route is False:
        return 0  # you cannot exit
    score = 50
    if impulse.liquidity_usd >= Decimal("500000"):
        score += 15
    elif impulse.liquidity_usd < Decimal("50000"):
        score -= 20
    if impulse.top10_pct > Decimal("70"):
        score -= 25
    elif impulse.top10_pct < Decimal("40"):
        score += 10
    if impulse.mint_renounced is True:
        score += 10
    elif impulse.mint_renounced is False:
        score -= 15
    if impulse.lp_locked is True:
        score += 12
    elif impulse.lp_locked is False:
        score -= 15
    if impulse.slippage_bps > 300:
        score -= 15
    elif impulse.slippage_bps < 100:
        score += 5
    if impulse.volume_1h_usd > impulse.liquidity_usd * Decimal("0.5"):
        score += 10
    taxes = (impulse.buy_tax_pct, impulse.sell_tax_pct)
    if any(tax is not None and tax > Decimal("10") for tax in taxes):
        score -= 20
    elif impulse.buy_tax_pct == 0 and impulse.sell_tax_pct == 0 and impulse.buy_route and impulse.sell_route:
        score += 8
    hostile = {flag for flag in impulse.risk_flags if flag.startswith("bytecode:")}
    score -= min(15, 5 * len(hostile))
    score += int(impulse.confidence * Decimal("15"))
    return max(0, min(100, score))
