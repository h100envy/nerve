from __future__ import annotations

from decimal import Decimal

from .models import ChainName, Impulse, NodeType, PoolObservation, Verdict
from .protocol import NerveNode
from .score import nerve_score

PAPER_BLOCK = 1_000_000


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


class PaperSentinel(NerveNode):
    """Fixed safe round-trip facts so paper-scan runs with no RPC and no key."""

    node_type = NodeType.SENTINEL
    owns = "stamps a fixed zero-tax round trip at a fixed paper block"
    boundary = "never opens an RPC connection, never signs, never asks the model"

    def head_block(self) -> int:
        return PAPER_BLOCK

    def process(self, impulse: Impulse) -> Impulse:
        if impulse.verdict is Verdict.REJECT:
            return impulse
        impulse.buy_tax_pct = impulse.sell_tax_pct = Decimal("0")
        impulse.metadata["sentinel"] = {
            "mode": "paper", "block_number": PAPER_BLOCK, "age_blocks": 0,
            "buy_leg": "ok", "sell_leg": "ok", "sources": {"simulation": "paper"},
        }
        impulse.score = nerve_score(impulse)
        return impulse.advance(NodeType.SENTINEL, Verdict.PASS, f"paper block={PAPER_BLOCK} buy_tax=0% sell_tax=0%")
