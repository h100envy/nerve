from __future__ import annotations

from collections.abc import Callable

from .models import Impulse, NodeType, PortfolioContext, Verdict
from .protocol import NerveNode
from .reflexes import Reflex, check_reflexes, default_reflexes
from .store import NerveStore


class Spine:
    """Conducts one impulse through a route and records every transition."""

    def __init__(
        self,
        nodes: list[NerveNode],
        store: NerveStore,
        context_fn: Callable[[], PortfolioContext],
        reflexes: tuple[Reflex, ...] | None = None,
    ) -> None:
        self.nodes = {node.node_type: node for node in nodes}
        # SENTINEL is free and deterministic, so it runs before the paid model
        # call and long before any signer.
        self.route = [NodeType.SCANNER, NodeType.SENTINEL, NodeType.ANALYST, NodeType.RISK, NodeType.EXECUTOR]
        self.store = store
        self.context_fn = context_fn
        self.reflexes = reflexes or default_reflexes()

    def add_node(self, node: NerveNode, after: NodeType | None = None) -> None:
        self.nodes[node.node_type] = node
        if node.node_type in self.route:
            return
        if after is not None and after in self.route:
            self.route.insert(self.route.index(after) + 1, node.node_type)
        else:
            self.route.append(node.node_type)

    def conduct(self, impulse: Impulse) -> Impulse:
        context = self.context_fn()
        impulse.metadata.setdefault("portfolio", context.model_dump(mode="json"))
        # Create the parent row before transition inserts (foreign key guard).
        self.store.save(impulse)
        for index, node_type in enumerate(self.route):
            if impulse.verdict is Verdict.REJECT:
                break
            # Scanner must first populate metrics; reflexes guard all expensive
            # and side-effecting nodes after that point.
            if index > 0:
                impulse = check_reflexes(impulse, self.context_fn(), self.reflexes, before=node_type)
                self.store.save(impulse)
                self.store.log_transition(impulse)
                if impulse.verdict is Verdict.REJECT:
                    break
            node = self.nodes.get(node_type)
            if node is None:
                continue
            impulse = node.process(impulse)
            # SCANNER assigns the stable pool id; persist before the FK-backed
            # transition row is written.
            self.store.save(impulse)
            self.store.log_transition(impulse)
        self.store.save(impulse)
        return impulse
