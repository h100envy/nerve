# NERVE Protocol: seven AI agents for memecoin trading on Robinhood Chain

Every day thousands of memecoins arrive on DEXes. A human can inspect one
contract, its holders, pool depth, LP state and sell path in fifteen minutes.
That does not scale to hundreds of pools per hour. NERVE turns that work into a
typed, auditable pipeline.

NERVE is a protocol rather than seven unrelated scripts. A single `Impulse` message
travels through seven specialized nodes. Deterministic reflexes can stop it before
an expensive model call or a transaction. The spine controls order. The store
keeps a replayable history.

The first product is a Pool Scanner for Robinhood Chain. `PoolSource` is a small
interface, so an indexer for Base or a Solana RPC reader can be plugged in
without changing the message, nodes or audit schema.

## Chain and broker are different layers

Robinhood Chain is an EVM L2. A trade is a signed transaction sent to a DEX
router. It is not a Robinhood brokerage order, and there is no `cancel_order`
after a transaction has entered a block.

| Broker API | On-chain NERVE |
| --- | --- |
| broker order | signed transaction |
| bid/ask book | AMM pool curve and price impact |
| cancel before fill | no cancellation after inclusion |
| platform session | wallet key or signer service |
| stop order at broker | MONITOR submits an exit swap |
| filled/rejected state | receipt status plus confirmations |

Robinhood Chain mainnet uses chain ID `4663`, ETH for gas and 100 ms target block
time. Testnet is `46630`. NERVE verifies the chain ID before creating a live
signer. The public RPC is suitable for experiments but rate-limited; production
should use an authenticated provider and explicit timeouts.

## Impulse is the wire format

The package uses Pydantic instead of a free-form chat payload. Decimal fields
keep money calculations exact and every transition is bounded to one node.

```python
class Impulse(BaseModel):
    id: str
    chain: ChainName | str
    token: str
    pool: str
    liquidity_usd: Decimal
    top10_pct: Decimal
    slippage_bps: int
    volume_1h_usd: Decimal
    mint_renounced: bool | None
    lp_locked: bool | None
    thesis: str = ""
    confidence: Decimal = Decimal("0")
    size_usd: Decimal = Decimal("0")
    tx_hash: str = ""
    verdict: Verdict = Verdict.PENDING
    history: list[Transition] = []

    def advance(self, node: NodeType, verdict: Verdict, note: str = "") -> "Impulse":
        self.verdict = verdict
        self.history.append(Transition(node=node, verdict=verdict, note=note[:1000]))
        return self

    @property
    def path(self) -> str:
        return " → ".join(
            f"{t.node.value}:{t.verdict.value}({t.note})" for t in self.history
        )
```

An incident is therefore a concrete path such as:

```text
scanner:pass(score=88) → analyst:pass(thesis) → risk:reject(daily loss limit)
```

The same object is stored in SQLite, so the path is available after a restart.

## Nodes have a code-level boundary

Every node implements one method and declares ownership and a boundary. The
base class rejects an incomplete subclass at definition time:

```python
class NerveNode(ABC):
    node_type: ClassVar[NodeType]
    owns: ClassVar[str]
    boundary: ClassVar[str]

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        for field in ("node_type", "owns", "boundary"):
            if not cls.__dict__.get(field):
                raise TypeError(f"{cls.__name__} must define {field}")

    @abstractmethod
    def process(self, impulse: Impulse) -> Impulse: ...
```

This makes “the analyst cannot execute” a property of the implementation. It is
not just a sentence in a prompt.

## Reflexes are faster than the cortex

Reflexes run after SCANNER and before SENTINEL, ANALYST, RISK and EXECUTOR. They
are pure functions over an impulse and current portfolio context:

```python
REFLEXES = (
    Reflex("kill_switch", lambda _i, c: c.kill_switch_active, "kill switch is active"),
    Reflex("daily_loss", lambda _i, c: c.daily_pnl_pct <= -c.daily_loss_limit_pct,
           "daily loss limit breached"),
    Reflex("gas_cap", lambda _i, c: c.gas_gwei > c.max_gas_gwei, "gas above cap"),
    Reflex("sell_simulation_failed", lambda i, _c: i.sell_route is False or sentinel_report(i).get("sell_leg") != "ok",
           "sell leg reverted, was not simulated, or sell_route is false", AFTER_SENTINEL),
    Reflex("buy_tax_above_cap", lambda i, c: i.buy_tax_pct is None or i.buy_tax_pct > c.max_buy_tax_pct,
           "buy tax unmeasured or above cap", AFTER_SENTINEL),
    Reflex("sell_tax_above_cap", lambda i, c: i.sell_tax_pct is None or i.sell_tax_pct > c.max_sell_tax_pct,
           "sell tax unmeasured or above cap", AFTER_SENTINEL),
    Reflex("stale_simulation", simulation_is_stale,
           "simulation block missing or older than SENTINEL_MAX_AGE_BLOCKS", AFTER_SENTINEL),
    Reflex("low_liquidity", lambda i, c: i.liquidity_usd < c.min_liquidity_usd,
           "liquidity below minimum"),
    Reflex("high_slippage", lambda i, c: i.slippage_bps > c.max_slippage_bps,
           "slippage above limit"),
)
```

The first match appends `risk:reject` and the spine stops. A kill switch thus
avoids both the Astra call and a signing side effect. The four SENTINEL reflexes
guard only the nodes behind SENTINEL, and an unmeasured value (`None`) trips them.

## The seven nodes

**SCANNER** consumes `PoolSource.discover()`. The Robinhood adapter reads V3
factory pools, `liquidity()` and `slot0()` for a reviewed token-address
allowlist. It calculates active-range WETH depth and does not pretend that a
pool contract contains holder concentration or 24-hour volume. Those fields
come from an indexer enrichment feed. Missing enrichment is unsafe and fails
closed. `ALLOW_ANY_TOKEN=true` switches the adapter to a bounded
`PoolCreated`-log window, still requiring enrichment before a signal can pass.

**SENTINEL** simulates the exit before the entry. At one pinned block it uses
`eth_simulateV1` to buy from the real wallet at `SENTINEL_SIMULATE_SIZE_USD`,
then sell exactly what the buy delivered in the next simulated block. A reverted
sell is a honeypot and is rejected. It measures buy and sell tax as the Quoter's
`amountOut` minus the observed balance delta, scans token bytecode for hostile
selectors, and derives `mint_renounced`, `lp_locked` and `top10_pct` on-chain,
falling back to `ENRICHMENT_PATH` only when a derivation is unavailable. It
calls no model, spends no gas and never signs. It runs before ANALYST because a
token that cannot be sold is not a question worth asking GPT. The Quoter alone
is not enough: it runs pool math from its own address and cannot see transfer
taxes, blacklists, whitelist gates, max-wallet limits, anti-bot cooldowns or a
disabled trading flag. See [sentinel.md](sentinel.md).

**ANALYST** receives normalized metrics only, including SENTINEL's measured
taxes, routes and bytecode flags. It uses the OpenAI Responses API
with a strict JSON Schema: `PASS | REJECT`, reason, thesis, confidence and risk
flags. It never sees private keys, transaction bytes, gas limits or position
size. If the API fails, the impulse is rejected.

**RISK** is deterministic. It checks the kill switch, daily loss, duplicate
positions and maximum positions, then sizes the trade from a loss-at-stop budget
and a notional cap. The model cannot change this result. The size is converted to
WETH only when an explicit WETH/USD reference is present.

**EXECUTOR** owns side effects. It creates an intent before sending, checks
allowance, builds `exactInputSingle`, signs locally and sends once. A timeout
after `send_raw_transaction` is marked `unknown`; it is reconciled by nonce and
hash instead of blindly resent. Approval is a separate idempotent transaction.

**MONITOR** owns exits. A stop is not stored by the chain: it is a process that
quotes the current position and submits an opposite swap when the stop, target
or liquidity-drain condition is reached. The kill switch blocks new entries; an
exit remains a separate, deliberate impulse.

**REPORTER** reads the audit store and reports the funnel, reverted swaps and gas
spent. A rising revert rate usually means the quote window or slippage cap needs
attention. Gas must be compared with P&L, otherwise a profitable-looking signal
can still lose money operationally.

## Spine and restart behaviour

```python
class Spine:
    route = [SCANNER, SENTINEL, ANALYST, RISK, EXECUTOR]

    def conduct(self, impulse: Impulse) -> Impulse:
        self.store.save(impulse)
        for index, node_type in enumerate(self.route):
            if index:
                impulse = check_reflexes(impulse, self.context_fn(), self.reflexes, before=node_type)
                self.store.log_transition(impulse)
                if impulse.verdict is Verdict.REJECT:
                    break
            impulse = self.nodes[node_type].process(impulse)
            self.store.log_transition(impulse)
            if impulse.verdict is Verdict.REJECT:
                break
        self.store.save(impulse)
        return impulse
```

Nodes do not own portfolio state. `NerveStore` persists impulses, transitions
and client intents. On restart, `nerve reconcile` resolves every `unknown` intent
by nonce and receipt, checks open positions against the wallet's token balance,
and exits non-zero on any divergence. It never sends a transaction. No
process-memory flag can cause a second buy.

## Robinhood Chain live path

The default Uniswap V3 addresses are configurable in `NerveConfig` and must be
verified against the [official deployment page](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments).
The first live path is:

```text
allowlisted token → factory.getPool → quoteExactInputSingle
  → SENTINEL round trip + tax + bytecode at one pinned block
  → deterministic reflexes → Astra thesis → deterministic risk
  → allowance/approve → amountOutMinimum → one signed swap
  → receipt + confirmations → store → monitor
```

The EVM adapter checks `chain_id`, derives the wallet from the key and verifies
the wallet address matches configuration. It uses the `pending` nonce so an
already submitted transaction cannot be overwritten. A short deadline and
`amountOutMinimum` protect the AMM leg; they do not make a honeypot safe. That is
SENTINEL's job, and it happens before the model is asked.

Never put the private key in source control, logs, an Impulse or a GPT prompt.
Use a secret manager or a separate signer process. Use a dedicated hot wallet
with a small balance and leave a native ETH reserve for exits and approvals.

## Runbook

1. Run `paper-scan` and inspect every `Impulse.path` in SQLite.
2. Run `chain-check` and `preflight` against testnet or a read-only production provider.
3. Enrich the allowlist with holder, LP-lock, mint and volume data from a
   trusted indexer. Do not infer these values from a token symbol.
4. Confirm the provider supports `eth_simulateV1` and inspect SENTINEL's round
   trip, measured taxes and `sources` for every allowlisted token.
5. Enable live mode only with `LIVE_TRADING_ENABLED=true` and a staffed window.
6. Run `nerve reconcile` before restarting an executor; restart only on exit 0.
7. Test MONITOR after a restart; on-chain stops do not run when the process is
   down.

NERVE provides an auditable execution skeleton. It does not promise returns and
does not replace contract review, simulation, provider monitoring or independent
strategy validation.
