from __future__ import annotations

from decimal import Decimal

from .models import ChainName, PoolObservation


def paper_observations() -> tuple[PoolObservation, ...]:
    return (
        PoolObservation(chain=ChainName.ROBINHOOD, token="0x1111111111111111111111111111111111111111", pool="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                         liquidity_usd=Decimal("900000"), volume_1h_usd=Decimal("800000"), volume_24h_usd=Decimal("6500000"),
                         top10_pct=Decimal("34"), slippage_bps=55, pool_age_hours=Decimal("4"), mint_renounced=True, lp_locked=True,
                         contract_verified=True, buy_route=True, sell_route=True),
        PoolObservation(chain=ChainName.ROBINHOOD, token="0x2222222222222222222222222222222222222222", pool="0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                         liquidity_usd=Decimal("180000"), volume_1h_usd=Decimal("40000"), volume_24h_usd=Decimal("800000"),
                         top10_pct=Decimal("81"), slippage_bps=420, pool_age_hours=Decimal("2"), mint_renounced=False, lp_locked=False,
                         contract_verified=True, buy_route=True, sell_route=True),
    )
