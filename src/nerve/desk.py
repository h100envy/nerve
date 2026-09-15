from __future__ import annotations

from decimal import Decimal

from .agents.analyst import AnalystNode
from .agents.executor import ExecutionAdapter, ExecutorNode
from .agents.monitor import MonitorNode
from .agents.reporter import ReporterNode
from .agents.risk import RiskNode
from .agents.scanner import ScannerNode
from .config import ExecutionMode, NerveConfig
from .models import ChainName, Impulse, PortfolioContext
from .protocol import NerveNode
from .sources import PoolSource, StaticPoolSource
from .spine import Spine
from .store import NerveStore


class NerveDesk:
    """Six nodes, one spine, and no mutable trading state in node memory."""

    def __init__(self, config: NerveConfig, source: PoolSource | None = None) -> None:
        self.config = config
        config.ensure_runtime_dirs()
        self.store = NerveStore(config.db_path)
        if source is None:
            if config.execution_mode is ExecutionMode.PAPER:
                from .paper import paper_observations

                source = StaticPoolSource(paper_observations())
            elif config.chain is ChainName.ROBINHOOD:
                from .adapters import RobinhoodChainPoolSource

                source = RobinhoodChainPoolSource(
                    rpc_url=config.rpc_url, token_allowlist=config.token_allowlist,
                    weth=config.weth_address, factory=config.factory_address, fees=config.pool_fees,
                    quoter=config.quoter_address, weth_usd=config.weth_usd,
                    allow_any_token=config.allow_any_token, scan_window_blocks=config.scan_window_blocks,
                    enrichment_path=config.enrichment_path,
                )
            else:
                raise ValueError("provide a PoolSource adapter for Base or Solana")
        self.scanner = ScannerNode(source)
        self.analyst = AnalystNode(api_key=config.openai_api_key, model=config.openai_model)
        self.risk = RiskNode(config.kill_switch_file, config.risk_per_trade_pct, config.max_position_pct)
        if config.execution_mode is ExecutionMode.PAPER:
            from .execution import PaperExecution

            execution: ExecutionAdapter = PaperExecution()
        else:
            from .execution import EvmExecution, EvmExecutionConfig

            execution = EvmExecution(EvmExecutionConfig(
                rpc_url=config.rpc_url, chain_id=config.chain_id, wallet_address=config.wallet_address,
                private_key=config.private_key, router_address=config.router_address,
                quoter_address=config.quoter_address, weth_address=config.weth_address,
                confirmations=config.confirmations, slippage_bps=config.max_slippage_bps,
                deadline_sec=config.deadline_sec, max_gas_gwei=float(config.max_gas_gwei),
            ))
        self.executor = ExecutorNode(execution, self.store)
        self.monitor = MonitorNode(config.kill_switch_file)
        self.reporter = ReporterNode(self.store)
        self.nodes: list[NerveNode] = [self.scanner, self.analyst, self.risk, self.executor]
        self.spine = Spine(self.nodes, self.store, self.context)

    def context(self) -> PortfolioContext:
        if self.config.execution_mode is ExecutionMode.LIVE:
            native, wrapped, gas = self.executor.adapter.wallet_snapshot()  # type: ignore[attr-defined]
            equity = Decimal(str(wrapped)) * self.config.weth_usd
            if equity <= 0:
                raise RuntimeError("live wallet has no WETH equity; refusing to size a trade")
            return PortfolioContext(
                equity_usd=equity, daily_pnl_pct=Decimal("0"), gas_gwei=Decimal(str(gas)),
                held_tokens=self.store.held_tokens(), open_positions=self.store.open_position_count(),
                native_balance=Decimal(str(native)), kill_switch_active=self.config.kill_switch_file.exists(),
                daily_loss_limit_pct=self.config.daily_loss_limit_pct, max_gas_gwei=self.config.max_gas_gwei,
                min_liquidity_usd=self.config.min_liquidity_usd, max_slippage_bps=self.config.max_slippage_bps,
                max_positions=self.config.max_positions, min_score=self.config.min_score,
            )
        return PortfolioContext(
            equity_usd=Decimal("100000"), daily_pnl_pct=Decimal("0"), gas_gwei=Decimal("0"),
            open_positions=0, native_balance=Decimal("1"), kill_switch_active=self.config.kill_switch_file.exists(),
            daily_loss_limit_pct=self.config.daily_loss_limit_pct, max_gas_gwei=self.config.max_gas_gwei,
            min_liquidity_usd=self.config.min_liquidity_usd, max_slippage_bps=self.config.max_slippage_bps,
            max_positions=self.config.max_positions, min_score=self.config.min_score,
        )

    def run_scan_cycle(self) -> list[Impulse]:
        result: list[Impulse] = []
        for impulse in self.scanner.discover():
            # The quote currency is an explicit input, never guessed by GPT.
            impulse.metadata.setdefault("weth_usd", str(self.config.weth_usd or Decimal("1")))
            result.append(self.spine.conduct(impulse))
        return result

    def monitor_cycle(self, impulse: Impulse) -> Impulse:
        return self.monitor.process(impulse)

    def report(self) -> str:
        return self.reporter.brief()

    def reconcile(self) -> list[dict[str, object]]:
        """Return intents whose network outcome still needs reconciliation."""
        return self.store.unknown_intents()

    def stop(self) -> None:
        self.monitor.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
        self.monitor.kill_switch_file.touch()

    def close(self) -> None:
        self.store.close()
