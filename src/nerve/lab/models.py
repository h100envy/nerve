from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..models import Impulse

DEFAULT_HORIZONS = (60, 300, 900, 1800)


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    """Serialize hash inputs without Python repr or ordering instability."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ObservationSnapshot(BaseModel):
    """An immutable copy of the post-SENTINEL facts supplied to every arm."""

    model_config = ConfigDict(frozen=True)

    snapshot_id: str
    impulse_id: str
    chain: str
    token: str
    pool: str
    observed_at: datetime
    captured_at: datetime
    stage: str = "post_sentinel"
    input_hash: str
    payload_json: str = Field(repr=False)

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached view; callers cannot mutate the stored snapshot."""
        value: dict[str, Any] = json.loads(self.payload_json)
        return value

    @classmethod
    def capture(
        cls,
        impulse: Impulse,
        *,
        captured_at: datetime | None = None,
        stage: str = "post_sentinel",
    ) -> ObservationSnapshot:
        captured = captured_at or utc_now()
        # JSON round-tripping creates a detached, normalized copy. No later
        # mutation of the Impulse can alter this payload.
        payload = json.loads(impulse.model_dump_json())
        payload_json = canonical_json(payload)
        input_hash = sha256(payload_json.encode()).hexdigest()
        identity = canonical_json(
            {
                "impulse_id": impulse.id,
                "observed_at": impulse.created_at.isoformat(),
                "captured_at": captured.isoformat(),
                "stage": stage,
                "input_hash": input_hash,
            }
        )
        return cls(
            snapshot_id=sha256(identity.encode()).hexdigest()[:24],
            impulse_id=impulse.id,
            chain=str(impulse.chain),
            token=impulse.token,
            pool=impulse.pool,
            observed_at=impulse.created_at,
            captured_at=captured,
            stage=stage,
            input_hash=input_hash,
            payload_json=payload_json,
        )


class DecisionStatus(StrEnum):
    COMPLETED = "completed"
    ABSTAINED = "abstained"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


class ExperimentDecision(BaseModel):
    """Audited output of one arm. It is not an execution instruction."""

    model_config = ConfigDict(frozen=True)

    decision_id: str = Field(default_factory=lambda: uuid4().hex)
    snapshot_id: str
    input_hash: str
    arm_id: str
    strategy_id: str
    provider: str | None = None
    requested_model_id: str | None = None
    returned_model_id: str | None = None
    question_version: str
    status: DecisionStatus
    started_at: datetime
    completed_at: datetime
    deadline_at: datetime
    applied_at: datetime | None = None
    latency_ms: int = Field(ge=0)
    raw_answer: dict[str, Any] | None = None
    reason: str = ""
    flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_clock_and_expiry(self) -> ExperimentDecision:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.applied_at is not None and self.applied_at < self.completed_at:
            raise ValueError("applied_at cannot precede completed_at")
        elapsed = int((self.completed_at - self.started_at).total_seconds() * 1000)
        if abs(self.latency_ms - elapsed) > 1:
            raise ValueError("latency_ms must match the decision clock")
        expired = self.completed_at > self.deadline_at or self.status is DecisionStatus.EXPIRED
        if expired:
            if self.applied_at is not None:
                raise ValueError("an expired decision cannot be applied")
            object.__setattr__(self, "status", DecisionStatus.EXPIRED)
        return self

    @property
    def usable(self) -> bool:
        return self.status in {DecisionStatus.COMPLETED, DecisionStatus.REJECTED}


class OutcomeStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    UNAVAILABLE = "unavailable"


class ForwardOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome_id: str = Field(default_factory=lambda: uuid4().hex)
    snapshot_id: str
    horizon_seconds: int = Field(gt=0)
    target_at: datetime
    resolved_at: datetime | None = None
    reference_price: Decimal | None = Field(default=None, gt=0)
    outcome_price: Decimal | None = Field(default=None, gt=0)
    return_pct: Decimal | None = None
    realized_label: bool | None = None
    source: str
    status: OutcomeStatus = OutcomeStatus.PENDING

    @model_validator(mode="after")
    def resolved_values_are_real(self) -> ForwardOutcome:
        values = (self.resolved_at, self.reference_price, self.outcome_price, self.return_pct)
        if self.status is OutcomeStatus.RESOLVED and any(value is None for value in values):
            raise ValueError("resolved outcomes require timestamps, prices, and return")
        if self.status is not OutcomeStatus.RESOLVED and any(
            value is not None for value in (self.outcome_price, self.return_pct, self.realized_label)
        ):
            raise ValueError("unresolved outcomes cannot contain fabricated results")
        return self


class ExecutionObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    observation_id: str = Field(default_factory=lambda: uuid4().hex)
    snapshot_id: str
    arm_id: str
    signal_price: Decimal | None = Field(default=None, gt=0)
    quote_at: datetime | None = None
    quote_price: Decimal | None = Field(default=None, gt=0)
    obtainable_quantity: Decimal | None = Field(default=None, ge=0)
    fee: Decimal | None = Field(default=None, ge=0)
    impact_bps: int | None = Field(default=None, ge=0)
    executable_entry: Decimal | None = Field(default=None, gt=0)
    executable_exit: Decimal | None = Field(default=None, gt=0)
    net_return: Decimal | None = None
    source: str
    status: str

    @model_validator(mode="after")
    def executable_values_require_a_fresh_quote(self) -> ExecutionObservation:
        if self.executable_entry is not None and any(
            value is None for value in (self.quote_at, self.quote_price, self.obtainable_quantity)
        ):
            raise ValueError("executable_entry requires a timestamped obtainable quote")
        return self
