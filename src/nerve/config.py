from __future__ import annotations

import os
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import ChainName


class ExecutionMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class NerveConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chain: ChainName = ChainName.ROBINHOOD
    chain_id: int = 4663
    rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    live_trading_enabled: bool = False
    private_key: str = Field(default="", repr=False)
    wallet_address: str = ""
    confirmations: int = 2

    # Uniswap V3 Robinhood Chain deployments. Verify bytecode and addresses
    # against the official deployment page before a live rollout.
    weth_address: str = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
    factory_address: str = "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"
    quoter_address: str = "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"
    router_address: str = "0xcaf681a66d020601342297493863e78c959e5cb2"
    token_allowlist: tuple[str, ...] = ()
    pool_fees: tuple[int, ...] = (100, 500, 3000, 10_000)
    weth_usd: Decimal = Decimal("0")

    risk_per_trade_pct: Decimal = Decimal("0.01")
    max_position_pct: Decimal = Decimal("0.05")
    max_positions: int = 5
    daily_loss_limit_pct: Decimal = Decimal("0.03")
    max_gas_gwei: Decimal = Decimal("1.0")
    min_liquidity_usd: Decimal = Decimal("250000")
    max_slippage_bps: int = 100
    min_score: int = 55
    min_confidence: Decimal = Decimal("0.70")
    order_timeout_sec: int = 90
    deadline_sec: int = 45
    scan_interval_sec: int = 30

    openai_api_key: str = Field(default="", repr=False)
    openai_model: str = "gpt-6-astra"
    openai_reasoning_effort: str = "low"
    db_path: Path = Path("data/nerve.db")
    log_path: Path = Path("data/nerve.jsonl")
    kill_switch_file: Path = Path("data/KILL_SWITCH")

    @field_validator("chain_id")
    @classmethod
    def supported_chain(cls, value: int) -> int:
        if value not in (4663, 46630):
            raise ValueError("use Robinhood Chain mainnet 4663 or testnet 46630")
        return value

    @field_validator("risk_per_trade_pct", "max_position_pct", "daily_loss_limit_pct", "min_confidence")
    @classmethod
    def fraction(cls, value: Decimal) -> Decimal:
        if not Decimal("0") < value < Decimal("1"):
            raise ValueError("fraction must be between 0 and 1")
        return value

    @field_validator("max_positions", "confirmations", "order_timeout_sec", "deadline_sec", "scan_interval_sec")
    @classmethod
    def positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("setting must be positive")
        return value

    @field_validator("max_slippage_bps", "min_score")
    @classmethod
    def non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("setting cannot be negative")
        return value

    @field_validator("token_allowlist")
    @classmethod
    def evm_addresses(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for address in value:
            if len(address) != 42 or not address.startswith("0x"):
                raise ValueError(f"invalid EVM token address: {address}")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def live_guards(self) -> NerveConfig:
        if self.execution_mode is ExecutionMode.LIVE:
            if self.chain is not ChainName.ROBINHOOD or self.chain_id != 4663:
                raise ValueError("live EVM adapter currently targets Robinhood Chain mainnet 4663")
            if not self.live_trading_enabled:
                raise ValueError("LIVE mode requires LIVE_TRADING_ENABLED=true")
            if not self.private_key or not self.wallet_address:
                raise ValueError("LIVE mode requires PRIVATE_KEY and WALLET_ADDRESS")
            if not self.token_allowlist:
                raise ValueError("LIVE mode requires explicit TOKEN_ALLOWLIST")
        return self

    @classmethod
    def from_env(cls) -> NerveConfig:
        load_dotenv()
        tokens = tuple(x.strip() for x in os.getenv("TOKEN_ALLOWLIST", "").split(",") if x.strip())
        fees = tuple(int(x.strip()) for x in os.getenv("POOL_FEES", "100,500,3000,10000").split(","))
        return cls(
            chain=ChainName(os.getenv("CHAIN", "robinhood").lower()),
            chain_id=int(os.getenv("CHAIN_ID", "4663")), rpc_url=os.getenv("RPC_URL", cls.model_fields["rpc_url"].default),
            execution_mode=ExecutionMode(os.getenv("EXECUTION_MODE", "paper").lower()),
            live_trading_enabled=os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true",
            private_key=os.getenv("PRIVATE_KEY", ""), wallet_address=os.getenv("WALLET_ADDRESS", ""),
            confirmations=int(os.getenv("CONFIRMATIONS", "2")),
            weth_address=os.getenv("WETH_ADDRESS", cls.model_fields["weth_address"].default),
            factory_address=os.getenv("UNISWAP_V3_FACTORY", cls.model_fields["factory_address"].default),
            quoter_address=os.getenv("UNISWAP_V3_QUOTER", cls.model_fields["quoter_address"].default),
            router_address=os.getenv("UNISWAP_V3_ROUTER", cls.model_fields["router_address"].default),
            token_allowlist=tokens, pool_fees=fees, weth_usd=Decimal(os.getenv("WETH_USD", "0")),
            risk_per_trade_pct=Decimal(os.getenv("RISK_PER_TRADE_PCT", "0.01")),
            max_position_pct=Decimal(os.getenv("MAX_POSITION_PCT", "0.05")),
            max_positions=int(os.getenv("MAX_POSITIONS", "5")), daily_loss_limit_pct=Decimal(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.03")),
            max_gas_gwei=Decimal(os.getenv("MAX_GAS_GWEI", "1.0")), min_liquidity_usd=Decimal(os.getenv("MIN_LIQUIDITY_USD", "250000")),
            max_slippage_bps=int(os.getenv("MAX_SLIPPAGE_BPS", "100")), min_score=int(os.getenv("MIN_SCORE", "55")),
            min_confidence=Decimal(os.getenv("MIN_CONFIDENCE", "0.70")), order_timeout_sec=int(os.getenv("ORDER_TIMEOUT_SEC", "90")),
            deadline_sec=int(os.getenv("DEADLINE_SEC", "45")), scan_interval_sec=int(os.getenv("SCAN_INTERVAL_SEC", "30")),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-6-astra"), openai_reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "low"),
            db_path=Path(os.getenv("DB_PATH", "data/nerve.db")), log_path=Path(os.getenv("LOG_PATH", "data/nerve.jsonl")),
            kill_switch_file=Path(os.getenv("KILL_SWITCH_FILE", "data/KILL_SWITCH")),
        )

    def ensure_runtime_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_switch_file.parent.mkdir(parents=True, exist_ok=True)
