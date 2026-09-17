# SENTINEL: simulate the exit before the entry

SENTINEL is the seventh NERVE node. It sits between SCANNER and ANALYST:

```text
SCANNER → SENTINEL → ANALYST → RISK → EXECUTOR → MONITOR → REPORTER
```

It is deterministic, calls no model, costs no gas (read-only RPC only) and fails
closed. SENTINEL is free, so it runs before the paid model call and long before
any signer. A token that cannot be sold is not a question worth asking GPT.

```python
class SentinelNode(NerveNode):
    node_type = NodeType.SENTINEL
    owns = "simulates a round trip from the real wallet and measures transfer tax"
    boundary = "never signs, never broadcasts, never asks the model, never guesses a missing value"
```

## The three gaps it closes

Line references are to commit `1780a9b`, the last six-node version.

### 1. Taxes the model was told about but never received

`Impulse` declares `buy_tax_pct` and `sell_tax_pct` (`models.py:71-72`), and so
does `PoolObservation` (`models.py:127-128`). `AnalystNode` passes both to the
model (`agents/analyst.py:52`). Nothing in the repository ever assigned them, so
they were always `None`, serialized as the string `"None"`. The model reasoned
about transfer taxes it never received.

### 2. The Quoter proves the pool, not your wallet

`_route_metrics` in `adapters.py` set `buy_route` and `sell_route` by calling the
Uniswap V3 Quoter. The Quoter runs `pool.swap` from the Quoter contract's own
address and reverts with the result. Tokens never move to or from your wallet,
so it cannot observe:

| Trap | Why the Quoter misses it |
| --- | --- |
| Fee-on-transfer tax | The pool computes `amountOut`; the token then delivers less without reverting. |
| Address blacklist | The Quoter is not the blacklisted address. |
| `onlyWhitelisted` gate | The gate checks the sender or recipient, never the Quoter's simulation. |
| Max-wallet limit | No balance is credited to your wallet. |
| Per-block anti-bot cooldown | No real transfer to or from your wallet happens. |
| `tradingEnabled` flipped after launch | Pool math does not call the token's transfer hook. |

A healthy quote on both sides is therefore consistent with a honeypot.

### 3. Manual safety facts and a score that ignored the rest

`mint_renounced`, `lp_locked`, `top10_pct` and `contract_verified` came from a
hand-maintained JSON file at `ENRICHMENT_PATH`. That data is manual and goes
stale. `score.py` ignored `buy_route`, `sell_route`, `buy_tax_pct` and
`sell_tax_pct` entirely.

## Method

Every check reads state at one pinned block number `N`, taken once at the start,
so the simulation, bytecode scan and enrichment are mutually consistent.

### Round-trip simulation

A plain `eth_call` cannot carry state from one call to the next, and a buy
followed by a sell needs exactly that. SENTINEL uses `eth_simulateV1`, the
multi-call, multi-block form of `eth_call`. It is still a read: nothing is
signed or broadcast and no gas is spent. Every call is sent `from` the
configured `WALLET_ADDRESS`, so blacklists, whitelists and max-wallet rules see
the real wallet.

The state override gives the wallet native ETH only. WETH and the router
allowance are then created by the wallet's own calls inside the simulation, so
SENTINEL assumes no token storage layout.

Bundle 1, simulated block `N+1`, learns what the buy delivers:

| # | Call | Purpose |
| --- | --- | --- |
| 0 | `WETH.deposit()` with `value = size` | fund WETH |
| 1 | `WETH.approve(router, max)` | set router allowance |
| 2 | `Quoter.quoteExactInputSingle(WETH→token, size)` | quoted `amountOut` |
| 3 | `token.balanceOf(wallet)` | balance before |
| 4 | `Router.exactInputSingle(WETH→token, recipient = wallet)` | the buy |
| 5 | `token.balanceOf(wallet)` | balance after |

Bundle 2 replays block `N+1` unchanged, then adds simulated block `N+2`:

| # | Call | Purpose |
| --- | --- | --- |
| 6 | `token.approve(router, max)` | a hostile token can revert here |
| 7 | `Quoter.quoteExactInputSingle(token→WETH, delivered)` | quoted sell output after the buy moved the price |
| 8 | `WETH.balanceOf(wallet)` | WETH before |
| 9 | `Router.exactInputSingle(token→WETH, delivered)` | the sell of exactly what was delivered |
| 10 | `WETH.balanceOf(wallet)` | WETH after |

The sell runs in the next simulated block, as a real exit would, so a
one-transaction-per-block anti-bot rule is not mistaken for a honeypot. If the
replayed buy does not deliver the same amount, the simulation is discarded as
non-deterministic.

`size = SENTINEL_SIMULATE_SIZE_USD / WETH_USD`, the size a real entry would use.

Outcomes:

| Observation | Result |
| --- | --- |
| buy reverts or delivers 0 | `buy_route = sell_route = false`, SENTINEL rejects |
| approve or sell reverts, or the sell returns 0 WETH | honeypot: `sell_route = false`, score 0, SENTINEL rejects with `sell_simulation reverted` |
| no wallet, no `WETH_USD`, no fee tier, RPC without `eth_simulateV1` | legs `unavailable`, taxes stay `None`, SENTINEL rejects |
| both legs succeed | taxes measured, SENTINEL passes to the reflexes |

On Uniswap V3 a token that taxes the transfer into the pool usually reverts the
sell, because the pool checks that it received the full `amountIn`. SENTINEL
reports that correctly: you cannot exit through this router.

### Tax measurement

```text
buy_tax_pct  = (Quoter amountOut(WETH→token) − token balance delta) / Quoter amountOut × 100
sell_tax_pct = (Quoter amountOut(token→WETH) − WETH balance delta)  / Quoter amountOut × 100
```

Both are rounded up to 0.01 and never flatter a token. A delta at or above the
quote is 0. A failed quote leaves the tax `None`.

### Bytecode scan

`eth_getCode(token, N)` plus, when the EIP-1967 implementation slot is set, the
implementation's code (and a `bytecode:upgradeable_proxy` flag). The code is
scanned for `PUSH4 <selector>` of:

| Flag | Selectors |
| --- | --- |
| `bytecode:blacklist` | `setBlacklist`, `blacklist`, `addToBlacklist`, `setBlacklisted`, `setBots`, `blockBots` |
| `bytecode:max_wallet` | `setMaxWallet`, `setMaxWalletSize`, `setMaxWalletAmount`, `setMaxTxAmount`, `setMaxTransactionAmount` |
| `bytecode:pause` | `pause`, `unpause`, `setPaused` |
| `bytecode:trading_toggle` | `setTradingEnabled`, `enableTrading`, `openTrading`, `setTradingOpen` |
| `bytecode:mint` | `mint(address,uint256)`, `mint(uint256)` |
| `bytecode:tax_setter` | `setTaxes`, `setFees`, `setTax`, `setBuyTax`, `setSellTax`, `updateFees` |
| `bytecode:fee_exclusion` | `excludeFromFee`, `excludeFromFees`, `setExcludedFromFee`, `includeInFee` |
| `bytecode:whitelist_gate` | `setWhitelist`, `addToWhitelist`, `setWhitelistEnabled`, `setOnlyWhitelisted` |
| `bytecode:transfer_hook` | `setTransferHook`, `setAntiBot`, `setAntiBotEnabled`, `setCooldownEnabled`, `setTransferDelayEnabled`, `setGuard` |

Presence is a flag, not a reject: many legitimate tokens have an owner. Flags
feed the score and the analyst's context. The analyst can add flags but cannot
remove them. A selector match is a hint only. A renamed function evades it, and
a dispatcher that does not use `PUSH4` is not matched.

### On-chain enrichment

| Field | Derivation | Fallback |
| --- | --- | --- |
| `mint_renounced` | `owner()`, then `getOwner()`; renounced when the zero or dead address | `ENRICHMENT_PATH`, else `None` |
| `lp_locked` | Pool `Mint` logs from pool creation to `N`. NFT positions are resolved through `UNISWAP_V3_POSITION_MANAGER` (`IncreaseLiquidity`, `positions`, `ownerOf`), direct positions through `pool.positions`. Locked when ≥ 95% of live liquidity is held by `LP_LOCKER_ALLOWLIST` or the zero/dead address. | `ENRICHMENT_PATH`, else `None` |
| `top10_pct` | `INDEXER_URL` if set, else a bounded `Transfer`-log window (36,000 blocks, chunked). Candidate balances are read at `N`, excluding the pool, zero and dead. | `ENRICHMENT_PATH`, else the scanner's pessimistic value |
| `contract_verified` | not derivable on-chain | adapter reads `ENRICHMENT_PATH` |

Two rules keep the derivations from being optimistic:

- **`lp_locked` needs the whole pool history.** If pool creation is outside the
  bounded window, earlier positions are invisible and the value is unavailable.
  Without `UNISWAP_V3_POSITION_MANAGER`, NFT holders cannot be resolved, so the
  value is unavailable unless every minter is already a locker.
- **`top10_pct` is an upper bound.** Supply held by addresses not seen in the
  window is added to the top 10, as if one unseen holder owned it. A short window
  can only make the number worse. If the window accounts for less than 90% of
  circulating supply, the value is unavailable.

The indexer contract is `GET INDEXER_URL?chain=<chain>&token=<address>&block=<N>`
returning `{"top10_pct": <0..100>}`.

`impulse.metadata["sentinel"]["sources"]` records where each value came from:
`owner_call`, `lp_positions`, `indexer`, `transfer_logs`, `enrichment_file` or
`unavailable`.

### Freshness

`impulse.metadata["sentinel"]` stores `block_number` (the pinned `N`),
`head_at_observation`, `age_blocks` and `observed_at`. The `stale_simulation`
reflex compares the pinned block with the chain head in the current
`PortfolioContext`. It trips when the head minus `N` exceeds
`SENTINEL_MAX_AGE_BLOCKS`, when there is no pinned block, or when the head is
unknown.

Robinhood Chain targets 100 ms blocks. `SENTINEL_MAX_AGE_BLOCKS=30` is about
three seconds, which one model call can exceed. Tune it to your provider and
model latency, and remember that a longer window trusts an older simulation.

## Reflexes and score

| Reflex | Trips when |
| --- | --- |
| `sell_simulation_failed` | the sell leg is not `ok` (reverted, empty, not run or unavailable) or `sell_route` is false |
| `buy_tax_above_cap` | `buy_tax_pct` is `None` or above `MAX_BUY_TAX_PCT` |
| `sell_tax_above_cap` | `sell_tax_pct` is `None` or above `MAX_SELL_TAX_PCT` |
| `stale_simulation` | pinned block missing, head unknown, or older than `SENTINEL_MAX_AGE_BLOCKS` |

These reflexes guard ANALYST, RISK and EXECUTOR. They do not run before SENTINEL
itself, because its facts do not exist yet. A route without a SENTINEL node
still trips them.

`nerve_score` returns 0 when `sell_route` is false. It subtracts 20 when either
tax is above 10%, adds 8 when both taxes are exactly 0 with both routes true, and
subtracts 5 per distinct `bytecode:*` flag, capped at 15.

## Configuration

```dotenv
MAX_BUY_TAX_PCT=5
MAX_SELL_TAX_PCT=5
SENTINEL_MAX_AGE_BLOCKS=30
SENTINEL_SIMULATE_SIZE_USD=500
LP_LOCKER_ALLOWLIST=
INDEXER_URL=
UNISWAP_V3_POSITION_MANAGER=
```

`WALLET_ADDRESS`, `WETH_USD` and an RPC that supports `eth_simulateV1` are
required for a live simulation. Paper mode uses `PaperSentinel`, which stamps
fixed zero-tax values at a fixed paper block with no RPC and no key.

## What the simulation still cannot see

- **Future state.** An owner can blacklist the wallet, raise taxes or disable
  trading after block `N`. The bytecode flags show who could, not whether they
  will.
- **Longer cooldowns.** The sell is simulated one block after the buy. A token
  that blocks sells for minutes looks like a honeypot, and SENTINEL rejects it.
  That is fail-closed, not wrong.
- **Size and path.** The round trip uses `SENTINEL_SIMULATE_SIZE_USD` and one fee
  tier through `UNISWAP_V3_ROUTER`. A larger RISK size or a different path is not
  proven.
- **Simulation-aware contracts.** A token can branch on `block.coinbase`,
  `tx.gasprice` or other values that differ between simulation and inclusion.
- **Verification.** `contract_verified` needs an explorer API; it is not an
  on-chain fact.
