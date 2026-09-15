from decimal import Decimal

import pytest

from nerve.agents.executor import ExecutorNode
from nerve.config import ExecutionMode, NerveConfig
from nerve.desk import NerveDesk
from nerve.models import ChainName, Impulse, NodeType, PoolObservation, PortfolioContext, Verdict
from nerve.protocol import NerveNode
from nerve.reflexes import check_reflexes, default_reflexes
from nerve.score import nerve_score
from nerve.store import NerveStore


def healthy() -> Impulse:
    return PoolObservation(
        chain=ChainName.ROBINHOOD, token="0xtoken", pool="0xpool", liquidity_usd=Decimal("600000"),
        volume_1h_usd=Decimal("400000"), volume_24h_usd=Decimal("1000000"), top10_pct=35,
        slippage_bps=50, mint_renounced=True, lp_locked=True, contract_verified=True,
        buy_route=True, sell_route=True,
    ).to_impulse()


def test_impulse_path_is_a_recoverable_decision_trace() -> None:
    impulse = healthy().advance(NodeType.SCANNER, Verdict.PASS, "score=90")
    impulse.advance(NodeType.RISK, Verdict.REJECT, "daily loss")
    assert impulse.path == "scanner:pass(score=90) → risk:reject(daily loss)"


def test_boundary_is_required_at_class_definition() -> None:
    with pytest.raises(TypeError, match="boundary"):
        class MissingBoundary(NerveNode):
            node_type = NodeType.SCANNER
            owns = "nothing"

            def process(self, impulse: Impulse) -> Impulse:
                return impulse


def test_reflexes_stop_before_model() -> None:
    context = PortfolioContext(
        equity_usd=Decimal("1000"), kill_switch_active=True,
    )
    result = check_reflexes(healthy(), context, default_reflexes())
    assert result.verdict is Verdict.REJECT
    assert "kill_switch" in result.history[-1].note


def test_score_is_deterministic_and_penalizes_risk() -> None:
    assert nerve_score(healthy()) == nerve_score(healthy())
    bad = healthy().model_copy(update={"top10_pct": Decimal("90"), "slippage_bps": 500, "lp_locked": False})
    assert nerve_score(bad) < nerve_score(healthy())


def test_executor_intent_is_idempotent(tmp_path) -> None:
    class Adapter:
        calls = 0

        def execute(self, impulse: Impulse) -> tuple[str, int | None]:
            self.calls += 1
            return "0xabc", 7

    store = NerveStore(tmp_path / "nerve.db")
    adapter = Adapter()
    node = ExecutorNode(adapter, store)
    first = healthy().advance(NodeType.RISK, Verdict.EXECUTE, "size")
    first = node.process(first)
    first_verdict = first.verdict
    second = node.process(first)
    assert first_verdict is Verdict.FILL
    assert second.verdict is Verdict.ALERT
    assert adapter.calls == 1
    store.close()


def test_paper_desk_runs_without_rpc(tmp_path) -> None:
    config = NerveConfig(
        execution_mode=ExecutionMode.PAPER, db_path=tmp_path / "desk.db",
        kill_switch_file=tmp_path / "KILL_SWITCH",
    )
    desk = NerveDesk(config)
    result = desk.run_scan_cycle()
    assert [item.verdict for item in result] == [Verdict.FILL, Verdict.REJECT]
    assert "fill=1" in desk.report()
    desk.close()
