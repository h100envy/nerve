from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Protocol

from .models import DecisionStatus, ExperimentDecision, ObservationSnapshot, utc_now


class ExperimentArm(Protocol):
    arm_id: str

    def evaluate(self, snapshot: ObservationSnapshot, deadline_at: datetime) -> ExperimentDecision: ...


class ControlArm:
    """A no-model baseline that records an explicit abstention."""

    arm_id = "CONTROL"

    def evaluate(self, snapshot: ObservationSnapshot, deadline_at: datetime) -> ExperimentDecision:
        started = utc_now()
        completed = utc_now()
        return ExperimentDecision(
            snapshot_id=snapshot.snapshot_id,
            input_hash=snapshot.input_hash,
            arm_id=self.arm_id,
            strategy_id="control-abstain-v1",
            question_version="control-v1",
            status=DecisionStatus.ABSTAINED,
            started_at=started,
            completed_at=completed,
            deadline_at=deadline_at,
            latency_ms=int((completed - started).total_seconds() * 1000),
            raw_answer={"action": "ABSTAIN"},
            reason="Phase 0 control performs no model call and cannot execute",
        )


class DecisionSink(Protocol):
    def record_experiment_decision(self, decision: ExperimentDecision) -> None: ...


class LabRunner:
    """Runs independent arms against one exact immutable input."""

    def __init__(self, sink: DecisionSink, arms: Iterable[ExperimentArm]) -> None:
        self.sink = sink
        self.arms = tuple(arms)

    def evaluate(
        self,
        snapshot: ObservationSnapshot,
        *,
        deadline_at: datetime | None = None,
    ) -> list[ExperimentDecision]:
        deadline = deadline_at or (utc_now() + timedelta(seconds=30))
        decisions: list[ExperimentDecision] = []
        for arm in self.arms:
            decision = arm.evaluate(snapshot, deadline)
            if decision.snapshot_id != snapshot.snapshot_id or decision.input_hash != snapshot.input_hash:
                raise ValueError("experiment arm did not use the supplied frozen snapshot")
            self.sink.record_experiment_decision(decision)
            decisions.append(decision)
        return decisions
