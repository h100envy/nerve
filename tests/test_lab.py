from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from nerve.agents.executor import ExecutorNode
from nerve.agents.scanner import ScannerNode
from nerve.lab.arms import LabRunner
from nerve.lab.metrics import (
    BinaryPrediction,
    binary_log_loss,
    brier_score,
    expected_calibration_error,
    reliability_bins,
)
from nerve.lab.models import (
    DecisionStatus,
    ExecutionObservation,
    ExperimentDecision,
    ObservationSnapshot,
    OutcomeStatus,
)
from nerve.lab.outcomes import DeterministicResolver, pending_outcomes
from nerve.models import ChainName, Impulse, NodeType, PoolObservation, PortfolioContext, Verdict
from nerve.protocol import NerveNode
from nerve.sources import StaticPoolSource
from nerve.spine import Spine
from nerve.store import NerveStore


def candidate() -> Impulse:
    return PoolObservation(
        chain=ChainName.ROBINHOOD,
        token="0xtoken",
        pool="0xpool",
        liquidity_usd=Decimal("600000"),
        volume_1h_usd=Decimal("400000"),
        volume_24h_usd=Decimal("1000000"),
        top10_pct=Decimal("35"),
        slippage_bps=50,
        mint_renounced=True,
        lp_locked=True,
        contract_verified=True,
        buy_route=True,
        sell_route=True,
    ).to_impulse()


def decision(
    snapshot: ObservationSnapshot,
    arm_id: str,
    *,
    started: datetime | None = None,
    completed: datetime | None = None,
    deadline: datetime | None = None,
    applied: datetime | None = None,
    raw_answer: dict[str, Any] | None = None,
    model_id: str | None = None,
    question_version: str = "q1",
) -> ExperimentDecision:
    start = started or datetime(2026, 1, 1, tzinfo=UTC)
    finish = completed or start + timedelta(milliseconds=20)
    return ExperimentDecision(
        snapshot_id=snapshot.snapshot_id,
        input_hash=snapshot.input_hash,
        arm_id=arm_id,
        strategy_id="test",
        requested_model_id=model_id,
        question_version=question_version,
        status=DecisionStatus.COMPLETED,
        started_at=start,
        completed_at=finish,
        deadline_at=deadline or start + timedelta(seconds=1),
        applied_at=applied,
        latency_ms=max(0, int((finish - start).total_seconds() * 1000)),
        raw_answer=raw_answer,
    )


def test_pool_identity_is_stable_but_observation_identity_is_unique() -> None:
    scanner = ScannerNode(StaticPoolSource(()))
    first = scanner.process(candidate())
    second = scanner.process(candidate())
    first_snapshot = ObservationSnapshot.capture(
        first, captured_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    second_snapshot = ObservationSnapshot.capture(
        second, captured_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    )
    assert first.id == second.id
    assert first_snapshot.snapshot_id != second_snapshot.snapshot_id


def test_snapshot_payload_is_detached_and_canonical() -> None:
    impulse = candidate()
    snapshot = ObservationSnapshot.capture(
        impulse, captured_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    original_hash = snapshot.input_hash
    impulse.metadata["later"] = True
    impulse.risk_flags.append("later")
    detached = snapshot.payload
    detached["metadata"]["attempted_mutation"] = True
    assert "later" not in snapshot.payload["metadata"]
    assert "attempted_mutation" not in snapshot.payload["metadata"]
    assert snapshot.input_hash == original_hash


def test_executor_client_id_and_intent_remain_pool_id_based(tmp_path: Any) -> None:
    class Adapter:
        calls = 0

        def execute(self, impulse: Impulse) -> tuple[str, int | None]:
            self.calls += 1
            return "0xabc", 7

    store = NerveStore(tmp_path / "client.db")
    scanner = ScannerNode(StaticPoolSource(()))
    first = scanner.process(candidate()).advance(NodeType.RISK, Verdict.EXECUTE, "size")
    second = scanner.process(candidate()).advance(NodeType.RISK, Verdict.EXECUTE, "size")
    assert ObservationSnapshot.capture(first).snapshot_id != ObservationSnapshot.capture(second).snapshot_id
    adapter = Adapter()
    executor = ExecutorNode(adapter, store)
    assert executor.process(first).verdict is Verdict.FILL
    assert executor.process(second).verdict is Verdict.ALERT
    assert store.get_intent(f"nerve-{first.id}") is not None
    assert adapter.calls == 1
    store.close()


def test_many_arms_receive_one_exact_snapshot(tmp_path: Any) -> None:
    class Arm:
        def __init__(self, arm_id: str) -> None:
            self.arm_id = arm_id

        def evaluate(
            self, snapshot: ObservationSnapshot, deadline_at: datetime
        ) -> ExperimentDecision:
            return decision(snapshot, self.arm_id, deadline=deadline_at)

    store = NerveStore(tmp_path / "arms.db")
    impulse = candidate()
    store.save(impulse)
    snapshot = ObservationSnapshot.capture(impulse)
    store.record_observation_snapshot(snapshot)
    decisions = LabRunner(store, (Arm("A"), Arm("B"))).evaluate(snapshot)
    assert {item.snapshot_id for item in decisions} == {snapshot.snapshot_id}
    assert {item.input_hash for item in decisions} == {snapshot.input_hash}
    assert {item.arm_id for item in store.list_experiment_decisions(snapshot.snapshot_id)} == {"A", "B"}
    with pytest.raises(ValueError, match="input_hash does not match"):
        store.record_experiment_decision(
            decision(snapshot, "C").model_copy(update={"input_hash": "wrong"})
        )
    store.close()


def test_decision_clock_rejects_impossible_times_and_expires_late_results() -> None:
    snapshot = ObservationSnapshot.capture(candidate())
    start = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="completed_at cannot precede"):
        decision(snapshot, "A", started=start, completed=start - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="applied_at cannot precede"):
        decision(snapshot, "A", started=start, applied=start)
    expired = decision(
        snapshot,
        "A",
        started=start,
        completed=start + timedelta(seconds=2),
        deadline=start + timedelta(seconds=1),
    )
    assert expired.status is DecisionStatus.EXPIRED
    assert expired.usable is False
    with pytest.raises(ValidationError, match="expired decision cannot be applied"):
        decision(
            snapshot,
            "A",
            started=start,
            completed=start + timedelta(seconds=2),
            deadline=start + timedelta(seconds=1),
            applied=start + timedelta(seconds=3),
        )


def test_expired_or_predecision_quote_cannot_be_recorded_as_executable(tmp_path: Any) -> None:
    store = NerveStore(tmp_path / "expired.db")
    impulse = candidate()
    store.save(impulse)
    snapshot = ObservationSnapshot.capture(impulse)
    store.record_observation_snapshot(snapshot)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    expired = decision(
        snapshot,
        "EXPIRED",
        started=start,
        completed=start + timedelta(seconds=2),
        deadline=start + timedelta(seconds=1),
    )
    store.record_experiment_decision(expired)
    expired_execution = ExecutionObservation(
        snapshot_id=snapshot.snapshot_id,
        arm_id="EXPIRED",
        quote_at=start + timedelta(seconds=3),
        quote_price=Decimal("10"),
        obtainable_quantity=Decimal("5"),
        executable_entry=Decimal("10"),
        source="test",
        status="quoted",
    )
    with pytest.raises(ValueError, match="expired experiment decision"):
        store.record_execution_observation(expired_execution)

    completed = decision(snapshot, "FRESH", started=start, completed=start + timedelta(seconds=1))
    store.record_experiment_decision(completed)
    stale_quote = expired_execution.model_copy(
        update={"observation_id": "stale", "arm_id": "FRESH", "quote_at": start}
    )
    with pytest.raises(ValueError, match="quote cannot precede"):
        store.record_execution_observation(stale_quote)
    store.close()


def test_unknown_measurements_stay_unknown_and_domains_stay_separate(tmp_path: Any) -> None:
    store = NerveStore(tmp_path / "unknown.db")
    impulse = candidate()
    store.save(impulse)
    snapshot = ObservationSnapshot.capture(impulse)
    store.record_observation_snapshot(snapshot)
    outcomes = pending_outcomes(snapshot)
    for outcome in outcomes:
        store.record_forward_outcome(outcome)
    execution = ExecutionObservation(
        snapshot_id=snapshot.snapshot_id,
        arm_id="CONTROL",
        source="not_quoted",
        status="unavailable",
    )
    store.record_execution_observation(execution)
    saved_outcomes = store.list_forward_outcomes(snapshot.snapshot_id)
    saved_execution = store.list_execution_observations(snapshot.snapshot_id)[0]
    assert [item.horizon_seconds for item in saved_outcomes] == [60, 300, 900, 1800]
    assert all(item.status is OutcomeStatus.PENDING for item in saved_outcomes)
    assert all(item.outcome_price is None and item.return_pct is None for item in saved_outcomes)
    assert saved_execution.quote_price is None
    assert saved_execution.obtainable_quantity is None
    assert saved_outcomes[0].snapshot_id == saved_execution.snapshot_id
    store.close()


def test_rejected_after_sentinel_candidate_keeps_snapshot_and_can_resolve_outcome(tmp_path: Any) -> None:
    class Sentinel(NerveNode):
        node_type = NodeType.SENTINEL
        owns = "test facts"
        boundary = "test only"

        def process(self, impulse: Impulse) -> Impulse:
            impulse.metadata["sentinel"] = {"sell_leg": "ok", "block_number": 10}
            impulse.buy_tax_pct = None
            impulse.sell_tax_pct = None
            return impulse.advance(NodeType.SENTINEL, Verdict.PASS, "facts captured")

    store = NerveStore(tmp_path / "rejected.db")
    spine = Spine(
        [ScannerNode(StaticPoolSource(())), Sentinel()],
        store,
        lambda: PortfolioContext(equity_usd=Decimal("1000"), head_block=10),
    )
    result = spine.conduct(candidate())
    assert result.verdict is Verdict.REJECT
    snapshots = store.list_observation_snapshots()
    assert len(snapshots) == 1
    pending = store.list_forward_outcomes(snapshots[0].snapshot_id)[0]
    resolved = DeterministicResolver(Decimal("10"), {60: Decimal("11")}).resolve(pending)
    store.record_forward_outcome(resolved)
    assert store.list_forward_outcomes(snapshots[0].snapshot_id)[0].return_pct == Decimal("10.0")
    events = store.list_forward_outcome_events(snapshots[0].snapshot_id)
    assert [item.status for item in events if item.horizon_seconds == 60] == [
        OutcomeStatus.PENDING,
        OutcomeStatus.RESOLVED,
    ]
    store.close()


def test_calibration_metrics_match_hand_computable_values() -> None:
    samples = [BinaryPrediction(0.25, False), BinaryPrediction(0.75, True)]
    assert brier_score(samples) == pytest.approx(0.0625)
    assert binary_log_loss(samples) == pytest.approx(-math.log(0.75))
    bins = reliability_bins(samples, bin_count=2)
    assert bins[0]["mean_probability"] == pytest.approx(0.25)
    assert bins[0]["positive_rate"] == 0
    assert bins[1]["mean_probability"] == pytest.approx(0.75)
    assert bins[1]["positive_rate"] == 1
    assert expected_calibration_error(samples, bin_count=2) == pytest.approx(0.25)


def test_calibration_groups_do_not_mix_model_question_or_horizon(tmp_path: Any) -> None:
    store = NerveStore(tmp_path / "calibration.db")
    for index, (model_id, question) in enumerate((("m1", "q1"), ("m2", "q2"))):
        impulse = candidate()
        impulse.created_at += timedelta(seconds=index)
        store.save(impulse)
        snapshot = ObservationSnapshot.capture(impulse)
        store.record_observation_snapshot(snapshot)
        store.record_experiment_decision(
            decision(
                snapshot,
                f"ARM-{index}",
                model_id=model_id,
                question_version=question,
                raw_answer={"probabilities": {"60": 0.8, "300": 0.6}},
            )
        )
        for horizon, price in ((60, Decimal("11")), (300, Decimal("9"))):
            pending = pending_outcomes(snapshot, (horizon,))[0]
            store.record_forward_outcome(
                DeterministicResolver(Decimal("10"), {horizon: price}).resolve(pending)
            )
    groups = store.calibration_samples()
    assert set(groups) == {("m1", "q1", 60), ("m1", "q1", 300), ("m2", "q2", 60), ("m2", "q2", 300)}
    assert all(len(samples) == 1 for samples in groups.values())
    store.close()
