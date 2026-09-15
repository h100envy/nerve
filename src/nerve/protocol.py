from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from .models import Impulse, NodeType


class NerveNode(ABC):
    """Every node declares ownership and a hard boundary."""

    node_type: ClassVar[NodeType]
    owns: ClassVar[str]
    boundary: ClassVar[str]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls is NerveNode:
            return
        for field in ("node_type", "owns", "boundary"):
            value = cls.__dict__.get(field)
            if not value:
                raise TypeError(f"{cls.__name__} must define {field}")

    @abstractmethod
    def process(self, impulse: Impulse) -> Impulse:
        """Accept one typed impulse and return it with one new transition."""
