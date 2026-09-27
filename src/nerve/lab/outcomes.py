from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from .models import DEFAULT_HORIZONS, ForwardOutcome, ObservationSnapshot, OutcomeStatus, utc_now


class PriceResolver(Protocol):
    def price_at(self, snapshot: ObservationSnapshot, target_at: datetime) -> Decimal | None: ...


def pending_outcomes(
    snapshot: ObservationSnapshot,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    *,
    source: str = "unconfigured",
) -> list[ForwardOutcome]:
    """Create explicit unknown horizons; no price is inferred from the Impulse."""
    return [
        ForwardOutcome(
            snapshot_id=snapshot.snapshot_id,
            horizon_seconds=horizon,
            target_at=snapshot.observed_at + timedelta(seconds=horizon),
            source=source,
            status=OutcomeStatus.PENDING,
        )
        for horizon in horizons
    ]


class DeterministicResolver:
    """Test/offline resolver backed by explicit prices, never by guessed market data."""

    def __init__(self, reference_price: Decimal, prices: dict[int, Decimal]) -> None:
        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        self.reference_price = reference_price
        self.prices = prices

    def resolve(self, outcome: ForwardOutcome) -> ForwardOutcome:
        price = self.prices.get(outcome.horizon_seconds)
        if price is None:
            return ForwardOutcome(
                snapshot_id=outcome.snapshot_id,
                horizon_seconds=outcome.horizon_seconds,
                target_at=outcome.target_at,
                source="deterministic",
                status=OutcomeStatus.UNAVAILABLE,
            )
        change = (price / self.reference_price - Decimal("1")) * Decimal("100")
        return ForwardOutcome(
            snapshot_id=outcome.snapshot_id,
            horizon_seconds=outcome.horizon_seconds,
            target_at=outcome.target_at,
            resolved_at=utc_now(),
            reference_price=self.reference_price,
            outcome_price=price,
            return_pct=change,
            realized_label=change > 0,
            source="deterministic",
            status=OutcomeStatus.RESOLVED,
        )
