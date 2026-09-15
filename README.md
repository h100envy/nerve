# NERVE Protocol

NERVE is a protocol for memecoin trading on DEXes. Six focused nodes pass one
typed message, `Impulse`, through a deterministic spine. Cheap reflexes stop a
rug, stale quote or exhausted risk budget before GPT or a signer is called.

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
cd "/Volumes/DEXP W500C/robinhood-agent"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
DB_PATH=/tmp/nerve.db KILL_SWITCH_FILE=/tmp/nerve-kill \
  .venv/bin/python -m nerve.cli paper-scan
.venv/bin/pytest -p no:cacheprovider
```

Paper mode uses synthetic observations and a paper fill adapter. It does not
open an RPC connection, import a key or call OpenAI. Set `OPENAI_API_KEY` to
replace the paper analyst with the Responses API and Structured Outputs.

## Package layout

```text
src/nerve/
├── models.py       # Impulse, Transition, observations and typed verdicts
├── protocol.py     # NerveNode contract and boundary enforcement
├── reflexes.py     # kill switch, loss, gas, liquidity, slippage and position gates
├── spine.py        # ordered routing and short-circuiting
├── score.py        # deterministic 0–100 NERVE score
├── sources.py      # PoolSource protocol and paper source
├── adapters.py     # Robinhood Chain Uniswap V3 read adapter
├── execution.py    # paper adapter and one-send EVM signer
├── store.py        # SQLite audit trail and idempotent intents
├── agents/         # scanner, analyst, risk, executor, monitor, reporter
└── desk.py         # composition root
```

## The impulse path

```text
PoolSource → SCANNER → reflexes → ANALYST (GPT-6 Astra)
          → reflexes → RISK → reflexes → EXECUTOR
          → MONITOR → REPORTER
```

An impulse is recoverable after a restart:

```text
scanner:pass(score=88) → analyst:pass(thesis) → risk:execute(size=$5000)
  → executor:fill(0x…)
```

`NerveNode` refuses a subclass without `node_type`, `owns` or `boundary` at
class definition time. This prevents a future generalist node from silently
crossing responsibilities.

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
rate-limited and intended for development.

The source returns `PoolObservation`. Holder concentration, volume, mint and LP
lock are enrichment fields, so an indexer must provide them. Missing enrichment
is represented as an unsafe value and fails closed; it is never guessed.

## GPT-6 Astra boundary

`AnalystNode` sends only normalized metrics. Its Structured Outputs schema is
`PASS | REJECT`, confidence, thesis and risk flags. It receives no private key,
RPC client, wallet address, transaction bytes, gas limit or position size. A
failed model call rejects the impulse. The risk and execution nodes are pure
code and do not ask the model for permission.

## Reflexes

The built-in reflex set runs before ANALYST, RISK and EXECUTOR:

- kill switch file;
- daily loss and maximum positions;
- gas cap;
- minimum liquidity and maximum quote slippage;
- duplicate token position.

The first fired reflex writes a `risk:reject` transition and the spine returns.
This is the expensive-call and side-effect boundary.

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
is marked `unknown` and reconciled by nonce and transaction hash. A revert means
gas was spent. Approvals are separate transactions and are also sent once.

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
```

Start with `paper-scan`, then run `chain-check` against a testnet or read-only
provider. Move to a staffed, minimum-size live test only after checking token
bytecode, pool depth, buy and sell routes, gas reserve, approval state, receipt
status and post-trade balances. The repository is an auditable starting point,
not a performance claim.
