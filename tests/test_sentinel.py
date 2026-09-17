import json
from decimal import Decimal
from typing import Any

import pytest

from nerve.agents.analyst import AnalystNode
from nerve.agents.executor import ExecutorNode
from nerve.agents.risk import RiskNode
from nerve.agents.scanner import ScannerNode
from nerve.agents.sentinel import SentinelNode, SentinelSettings
from nerve.chainread import READ_METHODS, JsonRpcReader, RpcError, selector
from nerve.config import ExecutionMode, NerveConfig
from nerve.desk import NerveDesk
from nerve.models import ChainName, Impulse, NodeType, PoolObservation, PortfolioContext, Verdict
from nerve.reflexes import check_reflexes, default_reflexes
from nerve.score import nerve_score
from nerve.sources import StaticPoolSource
from nerve.spine import Spine
from nerve.store import NerveStore

TOKEN = "0x1111111111111111111111111111111111111111"
POOL = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET = "0x9999999999999999999999999999999999999999"
BLOCK = 5_000
BUY_QUOTE = 1_000 * 10**18
SELL_QUOTE = 2 * 10**17


def word(value: int) -> str:
    return "0x" + value.to_bytes(32, "big").hex()


def ok(data: str = "0x") -> dict[str, str]:
    return {"status": "0x1", "returnData": data}


class FakeChain:
    """Deterministic JSON-RPC double. It records every method it was asked for."""

    def __init__(self, *, buy_tax: Decimal = Decimal("0"), sell_tax: Decimal = Decimal("0"),
                 sell_reverts: bool = False, head: int = BLOCK, code: bytes = b"\x60\x80") -> None:
        self.buy_tax, self.sell_tax, self.sell_reverts, self.head, self.code = buy_tax, sell_tax, sell_reverts, head, code
        self.methods: list[str] = []

    def request(self, method: str, params: list[Any]) -> Any:
        self.methods.append(method)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getCode":
            return "0x" + self.code.hex()
        if method == "eth_getStorageAt":
            return word(0)
        if method == "eth_call":
            return word(0)  # owner() == zero address
        if method == "eth_getLogs":
            return []
        if method == "eth_simulateV1":
            blocks = params[0]["blockStateCalls"]
            calls = self._simulate(sum(len(state["calls"]) for state in blocks))
            return [{"calls": calls[:6]}] + ([{"calls": calls[6:]}] if len(blocks) == 2 else [])
        raise AssertionError(f"unexpected RPC method {method}")

    def _simulate(self, count: int) -> list[dict[str, str]]:
        delivered = int(Decimal(BUY_QUOTE) * (1 - self.buy_tax / 100))
        calls = [ok(), ok(), ok(word(BUY_QUOTE)), ok(word(0)), ok(), ok(word(delivered))]
        if count == 11:
            weth_out = int(Decimal(SELL_QUOTE) * (1 - self.sell_tax / 100))
            sell = {"status": "0x0", "returnData": "0x", "error": {"message": "execution reverted: TRANSFER_FROM_FAILED"}} \
                if self.sell_reverts else ok()
            calls += [ok(), ok(word(SELL_QUOTE)), ok(word(10**18)), sell, ok(word(10**18 + weth_out))]
        return calls


def settings(**overrides: Any) -> SentinelSettings:
    values: dict[str, Any] = {
        "wallet_address": WALLET, "weth_address": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "router_address": "0xcaf681a66d020601342297493863e78c959e5cb2",
        "quoter_address": "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
        "factory_address": "0x1f7d7550b1b028f7571e69a784071f0205fd2efa", "weth_usd": Decimal("2500"),
    }
    return SentinelSettings(**{**values, **overrides})


def scanned() -> Impulse:
    impulse = PoolObservation(
        chain=ChainName.ROBINHOOD, token=TOKEN, pool=POOL, liquidity_usd=Decimal("900000"),
        volume_1h_usd=Decimal("800000"), volume_24h_usd=Decimal("6500000"), top10_pct=Decimal("34"),
        slippage_bps=55, mint_renounced=True, lp_locked=True, contract_verified=True,
        buy_route=True, sell_route=True, metadata={"fee": 3000},
    ).to_impulse()
    impulse.score = nerve_score(impulse)
    return impulse.advance(NodeType.SCANNER, Verdict.PASS, f"score={impulse.score}")


def context(head: int | None = BLOCK, **overrides: Any) -> PortfolioContext:
    return PortfolioContext(equity_usd=Decimal("100000"), head_block=head, **overrides)


class SpyAdapter:
    calls = 0

    def execute(self, impulse: Impulse) -> tuple[str, int | None]:
        self.calls += 1
        return "0xabc", 1


def spine_with(chain: FakeChain, tmp_path: Any, adapter: SpyAdapter) -> Spine:
    store = NerveStore(tmp_path / "sentinel.db")
    sentinel = SentinelNode(chain, settings())
    nodes = [ScannerNode(StaticPoolSource(())), sentinel, AnalystNode(), RiskNode(tmp_path / "KILL"),
             ExecutorNode(adapter, store)]
    return Spine(nodes, store, lambda: context(sentinel.head_block()))


def test_clean_round_trip_measures_zero_tax_and_passes() -> None:
    result = SentinelNode(FakeChain(), settings()).process(scanned())
    assert result.verdict is Verdict.PASS
    assert (result.buy_tax_pct, result.sell_tax_pct) == (Decimal("0"), Decimal("0"))
    assert result.metadata["sentinel"]["block_number"] == BLOCK
    assert result.metadata["sentinel"]["age_blocks"] == 0
    assert result.metadata["sentinel"]["sources"]["mint_renounced"] == "owner_call"
    assert check_reflexes(result, context(), default_reflexes()).verdict is Verdict.PASS


def test_honeypot_sell_leg_revert_is_rejected(tmp_path) -> None:
    adapter = SpyAdapter()
    impulse = PoolObservation(chain=ChainName.ROBINHOOD, token=TOKEN, pool=POOL, liquidity_usd=Decimal("900000"),
                              volume_1h_usd=Decimal("800000"), volume_24h_usd=Decimal("6500000"),
                              buy_route=True, sell_route=True, metadata={"fee": 3000}).to_impulse()
    result = spine_with(FakeChain(sell_reverts=True), tmp_path, adapter).conduct(impulse)
    assert result.verdict is Verdict.REJECT
    assert "sell_simulation" in result.history[-1].note
    assert result.sell_route is False
    assert nerve_score(result) == 0
    assert adapter.calls == 0
    # Even if a later route skipped the node's own verdict, the reflex holds.
    result.verdict = Verdict.PASS
    tripped = check_reflexes(result, context(), default_reflexes(), before=NodeType.ANALYST)
    assert "sell_simulation_failed" in tripped.history[-1].note


def test_tax_above_cap_is_rejected_by_reflex() -> None:
    result = SentinelNode(FakeChain(sell_tax=Decimal("12")), settings()).process(scanned())
    assert result.sell_tax_pct == Decimal("12.00")
    rejected = check_reflexes(result, context(), default_reflexes(), before=NodeType.ANALYST)
    assert rejected.verdict is Verdict.REJECT
    assert "sell_tax_above_cap" in rejected.history[-1].note


def test_buy_tax_is_quote_minus_delivered() -> None:
    result = SentinelNode(FakeChain(buy_tax=Decimal("7")), settings()).process(scanned())
    assert result.buy_tax_pct == Decimal("7.00")
    rejected = check_reflexes(result, context(), default_reflexes(), before=NodeType.RISK)
    assert "buy_tax_above_cap" in rejected.history[-1].note


def test_sell_route_false_scores_zero() -> None:
    impulse = scanned()
    assert nerve_score(impulse) > 0
    impulse.sell_route = False
    assert nerve_score(impulse) == 0


def test_score_uses_taxes_routes_and_bytecode_flags() -> None:
    base = scanned().model_copy(update={"top10_pct": Decimal("60"), "lp_locked": None, "mint_renounced": None})
    clean = base.model_copy(update={"buy_tax_pct": Decimal("0"), "sell_tax_pct": Decimal("0")})
    taxed = base.model_copy(update={"buy_tax_pct": Decimal("0"), "sell_tax_pct": Decimal("15")})
    flagged = clean.model_copy(update={"risk_flags": [f"bytecode:{n}" for n in ("mint", "pause", "blacklist", "max_wallet")]})
    assert nerve_score(clean) == nerve_score(base) + 8
    assert nerve_score(taxed) == nerve_score(base) - 20
    assert nerve_score(flagged) == nerve_score(clean) - 15


def test_none_taxes_trip_the_cap_reflex() -> None:
    impulse = scanned()
    impulse.metadata["sentinel"] = {"block_number": BLOCK, "buy_leg": "ok", "sell_leg": "ok"}
    assert impulse.buy_tax_pct is None and impulse.sell_tax_pct is None
    rejected = check_reflexes(impulse, context(), default_reflexes(), before=NodeType.ANALYST)
    assert rejected.verdict is Verdict.REJECT
    assert "buy_tax_above_cap" in rejected.history[-1].note


def test_unavailable_simulation_leaves_taxes_none_and_rejects() -> None:
    result = SentinelNode(FakeChain(), settings(wallet_address="")).process(scanned())
    assert result.verdict is Verdict.REJECT
    assert result.buy_tax_pct is None and result.sell_tax_pct is None
    assert "sell_simulation not run" in result.history[-1].note


def test_stale_block_trips_stale_simulation() -> None:
    result = SentinelNode(FakeChain(), settings()).process(scanned())
    fresh = check_reflexes(result.model_copy(deep=True), context(head=BLOCK + 30), default_reflexes(), before=NodeType.EXECUTOR)
    assert fresh.verdict is Verdict.PASS
    stale = check_reflexes(result, context(head=BLOCK + 31), default_reflexes(), before=NodeType.EXECUTOR)
    assert stale.verdict is Verdict.REJECT
    assert "stale_simulation" in stale.history[-1].note
    unknown_head = check_reflexes(result.model_copy(deep=True), context(head=None), default_reflexes(), before=NodeType.RISK)
    assert "stale_simulation" in unknown_head.history[-1].note


def test_sentinel_reflexes_do_not_fire_before_sentinel_runs() -> None:
    impulse = scanned()
    assert check_reflexes(impulse, context(), default_reflexes(), before=NodeType.SENTINEL).verdict is Verdict.PASS


def test_bytecode_selectors_become_flags_not_rejects() -> None:
    code = b"\x60\x80" + b"\x63" + selector("setBlacklist(address,bool)") + b"\x63" + selector("mint(address,uint256)")
    result = SentinelNode(FakeChain(code=code), settings()).process(scanned())
    assert result.verdict is Verdict.PASS
    assert {"bytecode:blacklist", "bytecode:mint"} <= set(result.risk_flags)


def test_paper_scan_completes_with_sentinel_in_route(tmp_path) -> None:
    config = NerveConfig(execution_mode=ExecutionMode.PAPER, db_path=tmp_path / "desk.db",
                         kill_switch_file=tmp_path / "KILL_SWITCH")
    desk = NerveDesk(config)
    route = desk.spine.route
    assert route.index(NodeType.SCANNER) + 1 == route.index(NodeType.SENTINEL) == route.index(NodeType.ANALYST) - 1
    filled, rejected = desk.run_scan_cycle()
    assert filled.verdict is Verdict.FILL
    assert filled.path.startswith("scanner:pass") and "→ sentinel:pass" in filled.path
    assert (filled.buy_tax_pct, filled.sell_tax_pct) == (Decimal("0"), Decimal("0"))
    assert rejected.verdict is Verdict.REJECT
    desk.close()


def test_sentinel_never_calls_a_signer(tmp_path) -> None:
    adapter = SpyAdapter()
    chain = FakeChain()
    node = SentinelNode(chain, settings())
    ExecutorNode(adapter, NerveStore(tmp_path / "spy.db"))
    node.process(scanned())
    assert adapter.calls == 0
    assert set(chain.methods) <= READ_METHODS
    assert not any("send" in method or "sign" in method for method in chain.methods)
    assert not any("private" in name or "key" in name for name in vars(node))
    with pytest.raises(RpcError, match="not a read method"):
        JsonRpcReader("http://127.0.0.1:9").request("eth_sendRawTransaction", ["0x00"])


class EnrichingChain(FakeChain):
    """Transfer logs for three holders; owner() reverts; LP creation outside the window."""

    HOLDERS = {"0x" + "a1" * 20: 300, "0x" + "b2" * 20: 200, "0x" + "c3" * 20: 50, POOL: 400}

    def request(self, method: str, params: list[Any]) -> Any:
        if method == "eth_call":
            self.methods.append(method)
            data = params[0]["data"]
            if data.startswith("0x" + selector("totalSupply()").hex()):
                return word(1_000)
            raise RpcError("execution reverted")
        if method == "eth_getLogs":
            self.methods.append(method)
            if params[0]["address"] != TOKEN:
                return []
            zero = "0x" + "00" * 32
            return [{"topics": ["0xt", zero, "0x" + "00" * 12 + holder[2:]]} for holder in self.HOLDERS]
        if method == "eth_simulateV1":
            calls = params[0]["blockStateCalls"][0]["calls"]
            if calls and calls[0]["data"].startswith("0x" + selector("balanceOf(address)").hex()) and calls[0]["to"] == TOKEN:
                self.methods.append(method)
                return [{"calls": [ok(word(self.HOLDERS.get("0x" + call["data"][-40:].lower(), 0))) for call in calls]}]
        return super().request(method, params)


def test_enrichment_derives_top10_upper_bound_and_falls_back_to_file(tmp_path) -> None:
    enrichment = tmp_path / "enrichment.json"
    enrichment.write_text(json.dumps({TOKEN: {"mint_renounced": False, "lp_locked": True, "top10_pct": 1}}))
    result = SentinelNode(EnrichingChain(), settings(enrichment_path=enrichment)).process(scanned())
    report = result.metadata["sentinel"]
    # 550 held by three holders + 50 unseen supply attributed to a top holder.
    assert result.top10_pct == Decimal("60.00")
    assert report["sources"] == {"top10_pct": "transfer_logs", "mint_renounced": "enrichment_file",
                                 "lp_locked": "enrichment_file"}
    assert result.mint_renounced is False and result.lp_locked is True
