![NERVE Protocol](docs/assets/nerve_banner.png)

[![CI](https://github.com/h100envy/nerve/actions/workflows/ci.yml/badge.svg)](https://github.com/h100envy/nerve/actions/workflows/ci.yml)

# NERVE Protocol

NERVE is a protocol for memecoin trading on DEXes. Seven focused nodes pass one
typed message, `Impulse`, through a deterministic spine. Cheap reflexes stop a
rug, honeypot, taxed token, stale quote or exhausted risk budget before GPT or a
signer is called.

The first production adapter targets Uniswap V3 on Robinhood Chain. The source
interface also accepts Base and Solana adapters without changing the protocol.
NERVE is an independent project and is not affiliated with Robinhood.

## Why this is a protocol

The biological metaphor maps directly to code:

| NERVE concept | Engineering guarantee |
| --- | --- |
| Nerve specialization | Every node declares what it owns and its boundary. |
| Reflex | Deterministic checks run before expensive or side-effecting nodes. |
| Impulse | One Pydantic message carries facts and a complete transition history. |
| Spine | A router controls order and short-circuits on `REJECT`. |
| NerveStore | SQLite persists impulses, transitions and execution intents. |
| Score | The same metrics always produce the same 0–100 score. |

The model can write a thesis and confidence. It cannot select a wallet, alter a
size, bypass a reflex or send a transaction.

## Quick start

```bash
git clone https://github.com/h100envy/nerve.git
cd nerve
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
DB_PATH=/tmp/nerve.db KILL_SWITCH_FILE=/tmp/nerve-kill .venv/bin/nerve paper-scan
DB_PATH=/tmp/nerve.db .venv/bin/nerve reconcile
.venv/bin/ruff check . && .venv/bin/mypy src && .venv/bin/pytest -q
```

Paper mode uses synthetic observations, a `PaperSentinel` with fixed zero-tax
round-trip values and a paper fill adapter. It does not open an RPC connection,
import a key or call OpenAI. Set `OPENAI_API_KEY` to replace the paper analyst
with the Responses API and Structured Outputs.

### `nerve reconcile`

Run it before every restart of a live executor:

```bash
.venv/bin/nerve reconcile && .venv/bin/nerve run
```

It is read-only. It never builds, signs or sends a transaction, and it does not
construct the signer.

- Every intent with status `unknown` is resolved by nonce. The executor records
  the nonce and the locally computed hash of an uncertain send. Nonce at or
  above the wallet's pending nonce: `never_sent`. Nonce consumed: the receipt of
  the recorded hash sets `filled` or `reverted`. In the mempool, missing nonce or
  a nonce used by another transaction: left `unknown` and reported.
- Every open position is checked against the wallet's on-chain token balance. A
  position the wallet does not back is reported loudly and is never corrected.
- It prints a table and exits `1` on any divergence, `2` if it cannot read the
  chain, `0` when everything is backed. An empty store needs no RPC.

## Package layout

```text
src/nerve/
├── models.py       # Impulse, Transition, observations and typed verdicts
├── protocol.py     # NerveNode contract and boundary enforcement
├── reflexes.py     # kill switch, loss, gas, simulation, tax, freshness, liquidity, slippage, position gates
├── spine.py        # ordered routing and short-circuiting
├── score.py        # deterministic 0–100 NERVE score
├── sources.py      # PoolSource protocol, paper source and enrichment file loader
├── adapters.py     # Robinhood Chain Uniswap V3 read adapter
├── chainread.py    # read-only JSON-RPC client; refuses signing and broadcast methods
├── rpc.py          # read retries, with exactly-once broadcast semantics
├── execution.py    # paper adapter and one-send EVM signer
├── reconcile.py    # read-only intent and position reconciliation
├── store.py        # SQLite audit trail and idempotent intents
├── agents/         # scanner, sentinel, analyst, risk, executor, monitor, reporter
└── desk.py         # composition root
```

## The impulse path

```text
PoolSource → SCANNER → reflexes → SENTINEL (eth_call only, no model, no gas)
          → reflexes → ANALYST (GPT-6 Astra)
          → reflexes → RISK → reflexes → EXECUTOR
          → MONITOR → REPORTER
```

SENTINEL is free and deterministic, so it runs before the paid model call and
long before any signer. A token that cannot be sold is not a question worth
asking GPT.

An impulse is recoverable after a restart:

```text
scanner:pass(score=88) → sentinel:pass(block=65452122 buy_tax=0% sell_tax=0% flags=0)
  → analyst:pass(thesis) → risk:execute(size=$5000) → executor:fill(0x…)
```

`NerveNode` refuses a subclass without `node_type`, `owns` or `boundary` at
class definition time. This prevents a future generalist node from silently
crossing responsibilities.

## SENTINEL: simulate the exit before the entry

SENTINEL exists because of three verifiable gaps in the six-node version:

1. **The model was told about taxes nobody measured.** `Impulse` declares
   `buy_tax_pct` and `sell_tax_pct`, and ANALYST passes both to the model.
   Nothing in the repository assigned them, so they were always `None`.
2. **The Quoter does not prove your wallet can trade.** `_route_metrics` calls
   the Uniswap V3 Quoter, which runs pool math from the Quoter's own address. It
   cannot observe fee-on-transfer taxes (the token delivers less than
   `amountOut` without reverting), address blacklists, `onlyWhitelisted` gates,
   max-wallet limits, per-block anti-bot cooldowns or a `tradingEnabled` flag
   flipped after launch.
3. **Safety facts were manual and stale, and the score ignored the rest.**
   `mint_renounced`, `lp_locked`, `top10_pct` and `contract_verified` came from a
   hand-maintained JSON file, and `score.py` ignored `buy_route`, `sell_route`
   and both tax fields.

The fix, all pinned to one block number so every check sees the same state:

- **Round trip from the real wallet.** `eth_simulateV1` (the multi-call form of
  `eth_call`) funds `WALLET_ADDRESS` with native ETH by state override, wraps it,
  approves the router, buys at `SENTINEL_SIMULATE_SIZE_USD`, and in the next
  simulated block sells exactly the amount the buy delivered. A reverted or
  empty sell is a honeypot: reject.
- **Measured tax.** Buy tax is the Quoter's `amountOut` minus the observed token
  balance delta. Sell tax is the sell quote minus the observed WETH delta. Both
  are written to the Impulse, rounded up.
- **Bytecode scan.** `eth_getCode` on the token (and its EIP-1967
  implementation) is scanned for `PUSH4` selectors of blacklist, max-wallet,
  pause, trading toggle, mint, tax setters, fee exclusion, whitelist gates and
  transfer hooks. Matches become `bytecode:*` risk flags, not rejects.
- **On-chain enrichment.** `mint_renounced` from `owner()`/`getOwner()`;
  `lp_locked` from LP position holders against `LP_LOCKER_ALLOWLIST`; `top10_pct`
  from `INDEXER_URL` or a bounded `Transfer`-log window with a conservative upper
  bound. `ENRICHMENT_PATH` is only the fallback when a derivation is unavailable.
- **Freshness.** The pinned block and observation age go into
  `impulse.metadata["sentinel"]`; a simulation older than
  `SENTINEL_MAX_AGE_BLOCKS` is not trusted downstream.

Anything SENTINEL cannot derive stays `None` and fails closed at a reflex. The
full method, including what the simulation still cannot see, is in
[docs/sentinel.md](docs/sentinel.md).

## Robinhood Chain adapter

Robinhood Chain mainnet is EVM-compatible, uses chain ID `4663`, and pays gas in
ETH. Testnet is `46630`. The adapter reads Uniswap V3 factory pools, active V3
liquidity and `slot0`; the token universe is an explicit contract-address
allowlist. Symbols are never used as identity.

The default addresses in `NerveConfig` are the published Uniswap V3 deployment
values and remain configurable. Verify bytecode and addresses against the
[official Uniswap Robinhood Chain deployment page](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-robinhood-chain-deployments)
before a live rollout. Use a managed provider such as Alchemy for production;
the [public Robinhood RPC](https://docs.robinhood.com/chain/connecting/) is
rate-limited and intended for development. SENTINEL needs a provider that
supports `eth_simulateV1`.

The source returns `PoolObservation`. Volume and contract verification are
enrichment fields from an indexer; holder concentration, mint and LP lock are
derived on-chain by SENTINEL when possible. Missing enrichment is represented as
an unsafe value and fails closed; it is never guessed. `ENRICHMENT_PATH` is a
JSON object keyed by token address. `ALLOW_ANY_TOKEN` enables bounded
`PoolCreated` log discovery; keep it false until contract review and a small
live allowlist are in place.

## GPT-6 Astra boundary

`AnalystNode` sends only normalized metrics, now including SENTINEL's measured
taxes, routes and bytecode flags. Its Structured Outputs schema is
`PASS | REJECT`, confidence, thesis and risk flags. The model may add risk flags;
it cannot erase SENTINEL's. It receives no private key, RPC client, wallet
address, transaction bytes, gas limit or position size. A failed model call
rejects the impulse. The risk and execution nodes are pure code and do not ask
the model for permission.

## Reflexes

The built-in reflex set runs before SENTINEL, ANALYST, RISK and EXECUTOR:

- kill switch file;
- daily loss and maximum positions;
- gas cap;
- `sell_simulation_failed`: the sell leg reverted, was not simulated, or
  `sell_route` is false;
- `buy_tax_above_cap` and `sell_tax_above_cap`: tax above `MAX_BUY_TAX_PCT` /
  `MAX_SELL_TAX_PCT`, or unmeasured (`None` trips the cap, it never passes);
- `stale_simulation`: the pinned block is missing or older than
  `SENTINEL_MAX_AGE_BLOCKS`;
- minimum liquidity and maximum quote slippage;
- duplicate token position;
- minimum deterministic NERVE score.

The four SENTINEL reflexes guard ANALYST, RISK and EXECUTOR. A route without a
SENTINEL node therefore rejects everything. The first fired reflex writes a
`risk:reject` transition and the spine returns. This is the expensive-call and
side-effect boundary.

## Score

`nerve_score` is pure: same facts, same number. On top of liquidity, holder
concentration, mint, LP lock, slippage and volume:

- `sell_route` false: hard floor, the score is `0`;
- buy or sell tax above 10%: −20;
- both taxes exactly 0 with both routes true: +8;
- each distinct `bytecode:*` flag: −5, capped at −15.

## Live execution contract

Live execution is opt-in and requires `EXECUTION_MODE=live`,
`LIVE_TRADING_ENABLED=true`, a separate hot wallet and a token allowlist. The
EVM adapter:

1. reads a fresh quote and computes `amountOutMinimum` from the configured
   slippage cap;
2. reads the `pending` nonce and builds an `exactInputSingle` call;
3. signs locally and calls `send_raw_transaction` exactly once;
4. waits for a receipt and configured confirmations;
5. records `tx_hash`, nonce and status in the store.

An RPC timeout after send is **unknown**, never a reason to resend. The intent
is marked `unknown` with its nonce and local hash, and `nerve reconcile`
resolves it. A revert means gas was spent. Approvals are separate transactions
and are also sent once.

There are no on-chain stop orders. MONITOR watches quotes and liquidity and
creates a separate exit impulse. A process restart must restore open positions
from the store and chain; in-memory state is not a source of truth.

## Environment

Copy `.env.example`. Keep `PRIVATE_KEY` in a secret manager or signer process;
never commit it or include it in an Impulse. For Robinhood Chain set:

```dotenv
CHAIN=robinhood
CHAIN_ID=4663
RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/<key>
TOKEN_ALLOWLIST=0xYourReviewedToken,0xAnotherReviewedToken
WETH_USD=2500
EXECUTION_MODE=paper
LIVE_TRADING_ENABLED=false
OPENAI_MODEL=gpt-6-astra

MAX_BUY_TAX_PCT=5
MAX_SELL_TAX_PCT=5
SENTINEL_MAX_AGE_BLOCKS=30
SENTINEL_SIMULATE_SIZE_USD=500
LP_LOCKER_ALLOWLIST=
INDEXER_URL=
UNISWAP_V3_POSITION_MANAGER=
```

Robinhood Chain targets 100 ms blocks, so `SENTINEL_MAX_AGE_BLOCKS=30` is about
three seconds. Size it against your provider latency and model call time, or
every impulse will be rejected as stale before RISK. Keep
`SENTINEL_SIMULATE_SIZE_USD` at or above the largest position RISK can size.

Start with `paper-scan`, then run `chain-check` and `preflight` against a
testnet or read-only provider. `preflight` checks chain ID, block/gas and
bytecode at the configured WETH, factory, quoter and router. Move to a staffed,
minimum-size live test only after checking token bytecode, pool depth, the
SENTINEL round trip, gas reserve, approval state, receipt status and post-trade
balances, and after `nerve reconcile` exits `0`. The repository is an auditable
starting point, not a performance claim.

## License

MIT. See [LICENSE](LICENSE).
