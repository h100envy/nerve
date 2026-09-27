from __future__ import annotations

from collections import defaultdict
from statistics import mean, median
from typing import Any

from .metrics import (
    BinaryPrediction,
    binary_log_loss,
    brier_score,
    expected_calibration_error,
    reliability_bins,
)


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * probability + 0.5)))
    return ordered[index]


def build_lab_report(store: Any) -> dict[str, Any]:
    snapshots = store.list_observation_snapshots()
    decisions = store.list_experiment_decisions()
    outcomes = store.list_forward_outcomes()
    executions = store.list_execution_observations()
    latencies = [float(item.latency_ms) for item in decisions]

    by_horizon: dict[int, list[float]] = defaultdict(list)
    for outcome in outcomes:
        if outcome.return_pct is not None:
            by_horizon[outcome.horizon_seconds].append(float(outcome.return_pct))
    forward = {
        str(horizon): {
            "sample_count": len(values),
            "mean_return_pct": mean(values) if values else None,
            "median_return_pct": median(values) if values else None,
            "hit_rate": (sum(value > 0 for value in values) / len(values)) if values else None,
        }
        for horizon, values in sorted(by_horizon.items())
    }
    for horizon in (60, 300, 900, 1800):
        forward.setdefault(str(horizon), {"sample_count": 0, "mean_return_pct": None, "median_return_pct": None, "hit_rate": None})

    groups = store.calibration_samples()
    calibration: list[dict[str, Any]] = []
    for key, raw_samples in sorted(groups.items()):
        samples = [BinaryPrediction(probability=probability, label=label) for probability, label in raw_samples]
        calibration.append(
            {
                "model_id": key[0],
                "question_version": key[1],
                "horizon_seconds": key[2],
                "sample_count": len(samples),
                "brier": brier_score(samples),
                "log_loss": binary_log_loss(samples),
                "ece": expected_calibration_error(samples),
                "reliability_bins": reliability_bins(samples),
            }
        )

    quoted = [item for item in executions if item.quote_price is not None and item.quote_at is not None]
    decision_clock = {(item.snapshot_id, item.arm_id): item.completed_at for item in decisions}
    delays = [
        (item.quote_at - decision_clock[(item.snapshot_id, item.arm_id)]).total_seconds()
        for item in quoted
        if item.quote_at is not None and (item.snapshot_id, item.arm_id) in decision_clock
    ]
    decays = [
        float((item.quote_price / item.signal_price - 1) * 100)
        for item in quoted
        if item.quote_price is not None and item.signal_price is not None
    ]
    executable = [item for item in executions if item.executable_entry is not None]
    net_returns = [float(item.net_return) for item in executable if item.net_return is not None]
    arm_comparison: dict[str, dict[str, Any]] = {}
    for arm_id in sorted({item.arm_id for item in decisions} | {item.arm_id for item in executions}):
        arm_decisions = [item for item in decisions if item.arm_id == arm_id]
        arm_executions = [item for item in executions if item.arm_id == arm_id]
        arm_net_returns = [
            float(item.net_return) for item in arm_executions if item.net_return is not None
        ]
        arm_comparison[arm_id] = {
            "decision_count": len(arm_decisions),
            "completed_count": sum(item.status.value == "completed" for item in arm_decisions),
            "expired_count": sum(item.status.value == "expired" for item in arm_decisions),
            "abstained_count": sum(item.status.value == "abstained" for item in arm_decisions),
            "execution_observation_count": len(arm_executions),
            "quoted_count": sum(item.quote_price is not None for item in arm_executions),
            "executable_count": sum(item.executable_entry is not None for item in arm_executions),
            "net_return_mean": mean(arm_net_returns) if arm_net_returns else None,
        }
    return {
        "snapshots": {
            "count": len(snapshots),
            "unique_pools": len({(item.chain, item.pool) for item in snapshots}),
            "unique_tokens": len({(item.chain, item.token) for item in snapshots}),
        },
        "decisions": {
            "count": len(decisions),
            "arm_sample_count": dict(sorted(store.arm_sample_counts().items())),
            "expired_count": sum(item.status.value == "expired" for item in decisions),
            "abstention_rejection_count": sum(item.status.value in {"abstained", "rejected"} for item in decisions),
            "latency_ms_p50": percentile(latencies, 0.50),
            "latency_ms_p95": percentile(latencies, 0.95),
        },
        "forward_returns": dict(sorted(forward.items(), key=lambda item: int(item[0]))),
        "calibration": calibration,
        "arm_comparison": arm_comparison,
        "execution_reality": {
            "observation_count": len(executions),
            "quote_availability": len(quoted) / len(executions) if executions else None,
            "signal_to_quote_delay_seconds_mean": mean(delays) if delays else None,
            "signal_to_quote_decay_pct_mean": mean(decays) if decays else None,
            "executable_sample_count": len(executable),
            "net_return_mean": mean(net_returns) if net_returns else None,
        },
    }
