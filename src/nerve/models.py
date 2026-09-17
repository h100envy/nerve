from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class NodeType(StrEnum):
    SCANNER = "scanner"
    SENTINEL = "sentinel"
    ANALYST = "analyst"
    RISK = "risk"
    EXECUTOR = "executor"
    MONITOR = "monitor"
    REPORTER = "reporter"


class ChainName(StrEnum):
    ROBINHOOD = "robinhood"
    BASE = "base"
    SOLANA = "solana"


class Verdict(StrEnum):
    PENDING = "pending"
    PASS = "pass"
    REJECT = "reject"
    EXECUTE = "execute"
    FILL = "fill"
    REVERT = "revert"
    STOP = "stop"
    ALERT = "alert"


class Transition(BaseModel):
    node: NodeType
    verdict: Verdict
    note: str = ""
    ts: datetime = Field(default_factory=utc_now)


class Impulse(BaseModel):
    """The only message that can cross a NERVE node boundary."""

    model_config = ConfigDict(validate_assignment=True)

    id: str = Field(default_factory=lambda: uuid4().hex[:12], min_length=8, max_length=64)
    chain: ChainName | str = ChainName.ROBINHOOD
    token: str = ""
    pool: str = ""

    # scanner/enricher facts
    liquidity_usd: Decimal = Field(default=Decimal("0"), ge=0)
    top10_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    slippage_bps: int = Field(default=0, ge=0)
    volume_1h_usd: Decimal = Field(default=Decimal("0"), ge=0)
    volume_24h_usd: Decimal = Field(default=Decimal("0"), ge=0)
    pool_age_hours: Decimal = Field(default=Decimal("0"), ge=0)
    mint_renounced: bool | None = None
    lp_locked: bool | None = None
    contract_verified: bool = False
    buy_route: bool = False
    sell_route: bool = False
    buy_tax_pct: Decimal | None = Field(default=None, ge=0, le=100)
    sell_tax_pct: Decimal | None = Field(default=None, ge=0, le=100)
    price_change_1h_pct: Decimal = Decimal("0")

    # analyst/risk/execution facts
    thesis: str = ""
    confidence: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    risk_flags: list[str] = Field(default_factory=list)
    size_usd: Decimal = Field(default=Decimal("0"), ge=0)
    entry_price: Decimal = Field(default=Decimal("0"), ge=0)
    stop_price: Decimal = Field(default=Decimal("0"), ge=0)
    take_profit_price: Decimal = Field(default=Decimal("0"), ge=0)
    tx_hash: str = ""
    nonce: int | None = Field(default=None, ge=0)
    score: int = Field(default=0, ge=0, le=100)
    verdict: Verdict = Verdict.PENDING
    history: list[Transition] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("token", "pool")
    @classmethod
    def compact_identifier(cls, value: str) -> str:
        return value.strip()

    def advance(self, node: NodeType, verdict: Verdict, note: str = "") -> Impulse:
        self.verdict = verdict
        self.history.append(Transition(node=node, verdict=verdict, note=note[:1000]))
        return self

    @property
    def path(self) -> str:
        return " → ".join(
            f"{item.node.value}:{item.verdict.value}"
            + (f"({item.note})" if item.note else "")
            for item in self.history
        )


class PoolObservation(BaseModel):
    """Normalized output expected from a chain/indexer adapter."""

    chain: ChainName | str
    token: str
    pool: str
    liquidity_usd: Decimal = Field(ge=0)
    volume_1h_usd: Decimal = Field(ge=0)
    volume_24h_usd: Decimal = Field(ge=0)
    top10_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    slippage_bps: int = Field(default=0, ge=0)
    pool_age_hours: Decimal = Field(default=Decimal("0"), ge=0)
    mint_renounced: bool | None = None
    lp_locked: bool | None = None
    contract_verified: bool = False
    buy_route: bool = False
    sell_route: bool = False
    buy_tax_pct: Decimal | None = Field(default=None, ge=0, le=100)
    sell_tax_pct: Decimal | None = Field(default=None, ge=0, le=100)
    price_change_1h_pct: Decimal = Decimal("0")
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_impulse(self) -> Impulse:
        return Impulse(**self.model_dump())


class AnalystVerdict(BaseModel):
    verdict: str
    reason: str
    thesis: str = ""
    confidence: Decimal = Field(ge=0, le=1)
    risk_flags: list[str] = Field(default_factory=list)


class PortfolioContext(BaseModel):
    equity_usd: Decimal = Field(gt=0)
    daily_pnl_pct: Decimal = Decimal("0")
    gas_gwei: Decimal = Decimal("0")
    held_tokens: set[str] = Field(default_factory=set)
    open_positions: int = Field(default=0, ge=0)
    native_balance: Decimal = Field(default=Decimal("0"), ge=0)
    kill_switch_active: bool = False
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    max_gas_gwei: Decimal = Decimal("1.0")
    min_liquidity_usd: Decimal = Decimal("250000")
    max_slippage_bps: int = 100
    max_positions: int = 5
    min_score: int = 55
    max_buy_tax_pct: Decimal = Decimal("5")
    max_sell_tax_pct: Decimal = Decimal("5")
    sentinel_max_age_blocks: int = 30
    # Chain head when the context was taken. None means freshness is unprovable.
    head_block: int | None = None
