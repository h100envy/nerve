from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

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
