from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypedDict


@dataclass(frozen=True)
class BinaryPrediction:
    probability: float
    label: bool

    def __post_init__(self) -> None:
        if not 0.0 <= self.probability <= 1.0:
            raise ValueError("probability must be between zero and one")


class ReliabilityBin(TypedDict):
    lower: float
    upper: float
    count: int
    mean_probability: float | None
    positive_rate: float | None


def _samples(values: Iterable[BinaryPrediction]) -> list[BinaryPrediction]:
    result = list(values)
    if not result:
        raise ValueError("at least one prediction is required")
    return result


def brier_score(values: Iterable[BinaryPrediction]) -> float:
    samples = _samples(values)
    return sum((item.probability - float(item.label)) ** 2 for item in samples) / len(samples)


def binary_log_loss(values: Iterable[BinaryPrediction], epsilon: float = 1e-15) -> float:
    samples = _samples(values)
    total = 0.0
    for item in samples:
        probability = min(1.0 - epsilon, max(epsilon, item.probability))
        label = float(item.label)
        total += -(label * math.log(probability) + (1.0 - label) * math.log(1.0 - probability))
    return total / len(samples)


def reliability_bins(values: Iterable[BinaryPrediction], bin_count: int = 10) -> list[ReliabilityBin]:
    if bin_count <= 0:
        raise ValueError("bin_count must be positive")
    samples = _samples(values)
    buckets: list[list[BinaryPrediction]] = [[] for _ in range(bin_count)]
    for item in samples:
        index = min(int(item.probability * bin_count), bin_count - 1)
        buckets[index].append(item)
    result: list[ReliabilityBin] = []
    for index, bucket in enumerate(buckets):
        count = len(bucket)
        result.append(
            {
                "lower": index / bin_count,
                "upper": (index + 1) / bin_count,
                "count": count,
                "mean_probability": (
                    sum(item.probability for item in bucket) / count if count else None
                ),
                "positive_rate": (sum(float(item.label) for item in bucket) / count if count else None),
            }
        )
    return result


def expected_calibration_error(values: Iterable[BinaryPrediction], bin_count: int = 10) -> float:
    samples = _samples(values)
    bins = reliability_bins(samples, bin_count)
    return sum(
        item["count"] / len(samples) * abs(item["positive_rate"] - item["mean_probability"])
        for item in bins
        if item["count"] and item["positive_rate"] is not None and item["mean_probability"] is not None
    )
