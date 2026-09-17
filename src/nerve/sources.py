from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from .models import Impulse, PoolObservation


class PoolSource(Protocol):
    """Indexers, Web3 log readers and Solana RPC adapters implement this."""

    def discover(self) -> Iterable[Impulse]: ...


class StaticPoolSource:
    """Deterministic paper source used by tests and local dry runs."""

    def __init__(self, observations: Iterable[PoolObservation]) -> None:
        self.observations = tuple(observations)

    def discover(self) -> list[Impulse]:
        return [observation.to_impulse() for observation in self.observations]


def load_enrichment(path: Path | None) -> dict[str, dict[str, Any]]:
    """Read the hand-maintained enrichment file, keyed by lowercase token address."""
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("ENRICHMENT_PATH must contain an object keyed by token address")
    return {str(key).lower(): value for key, value in payload.items() if isinstance(value, dict)}
