# Lighter Adapter

Adapter to connect the [Lighter](https://apidocs.lighter.xyz) perpetual DEX to
Nautilus Trader. It provides venue‑native market data, execution, and
reconciliation logic following the same patterns as the built‑in CEX adapters
(Binance/Bybit/OKX, etc.).

This document is aimed at users of the adapter. Design notes and deep‑dive
documents are kept in separate markdown files (see “Further reading”).

---

## Status

- Venue: Lighter mainnet and testnet supported.
- Instruments: USDC‑quoted perpetuals (e.g. `BNBUSDC-PERP.LIGHTER`).
- Position mode: NETTING (single net position per instrument).
- Order types:
  - LIMIT / MARKET
  - Post‑only, reduce‑only
  - Basic TP/SL (stop / take‑profit, limit or market), mapped to Lighter types.
- Reconciliation:
  - Initial account, positions, and open orders via HTTP snapshot.
  - Continuous open‑check / in‑flight checks via `LiveExecutionEngine`.

---

## Requirements

- A working `nautilus_trader` installation (including compiled Rust/PyO3
  extensions).
- Python 3.11+ (matching the Nautilus environment).
- A Lighter account with API keys:
  - public key
  - private key
  - account index
  - API key index
- A signer shared library compatible with your platform (see below).

---

## Signer Library

Lighter orders and private HTTP calls are signed via a shared library. The
adapter expects a platform‑specific binary under:

```text
adapters/lighter/signers/
  signer-amd64.so        # Linux x86_64
  signer-arm64.so        # Linux arm64 (if used)
  signer-arm64.dylib     # macOS arm64
  signer-amd64.dll       # Windows x86_64
```

The `LighterSigner` will first honor an explicit `LIGHTER_SIGNER_LIB` path if
set, and otherwise look for the appropriate file under `signers/`. We do **not**
ship this binary in the repository. You can either:

- build it yourself from the official lighter‑python / lighter‑go projects; or
- obtain a binary from your own infrastructure.

To simplify installation, a small helper script is provided:

```bash
PYTHONPATH=/path/to/site-packages \
python adapters/lighter/scripts/get_signer_binary.py \
  --src /tmp/lighter-python-latest/lighter/signers/lighter-signer-linux-amd64.so

# or, if you host the .so somewhere:
PYTHONPATH=/path/to/site-packages \
python adapters/lighter/scripts/get_signer_binary.py \
  --url https://example.com/path/to/lighter-signer-linux-amd64.so
```
---

## Configuration

The adapter and scripts obtain credentials and endpoints from environment
variables. Typical settings:

```bash
export LIGHTER_PUBLIC_KEY="0x..."
export LIGHTER_PRIVATE_KEY="0x..."          # keep this secret
export LIGHTER_ACCOUNT_INDEX=XXXXX         # or your own account index
export LIGHTER_API_KEY_INDEX=2              # or your own API key index

# Optional – override defaults if needed:
export LIGHTER_HTTP_URL="https://mainnet.zklighter.elliot.ai"
export LIGHTER_WS_URL="wss://mainnet.zklighter.elliot.ai/stream"
```

To check that variables are set without printing secrets:

```bash
if [ -n "$LIGHTER_PUBLIC_KEY" ]; then echo "PUBLIC_KEY is set"; fi
if [ -n "$LIGHTER_PRIVATE_KEY" ]; then echo "PRIVATE_KEY is set"; fi
if [ -n "$LIGHTER_ACCOUNT_INDEX" ]; then echo "ACCOUNT_INDEX is set"; fi
```

> Never commit real keys or private keys to the repository. All examples use
> placeholders only.

---

## Quick Start

This section shows how to smoke‑test the adapter and then run a minimal live
`TradingNode` using only `LighterExecutionClient` and `LighterDataClient`.

Assume your Nautilus environment is at:

```text
/root/myenv/lib/python3.12/site-packages
```

Adjust `PYTHONPATH` accordingly for your setup.

### 1. Basic execution smoke test (no strategy)

Use `exec_client_smoke.py` to confirm signer, HTTP, WS, and private channels
all work end‑to‑end.

```bash
cd /root/myenv/lib/python3.12/site-packages
PYTHONPATH=/root/myenv/lib/python3.12/site-packages \
LIGHTER_PUBLIC_KEY="..." \
LIGHTER_PRIVATE_KEY="..." \
LIGHTER_ACCOUNT_INDEX=XXXXX \
LIGHTER_API_KEY_INDEX=2 \
python adapters/lighter/scripts/lighter_tests/exec_client_smoke.py
```

This script:

- builds a `LighterExecutionClient` directly (no `TradingNode`),
- fetches account balances via `/api/v1/account`,
- places a small test order on a chosen instrument,
- waits for `OrderAccepted` / `OrderCanceled` or `OrderFilled`,
- logs private WS callbacks (`account_all_*`).

It uses **only** the adapter code, not the official Lighter SDK.

### 2. Minimal live node with place/cancel

To see the adapter running under the Nautilus live engine, use
`live_lighter_simple_place_cancel.py`:

```bash
cd /root/myenv/lib/python3.12/site-packages
PYTHONPATH=/root/myenv/lib/python3.12/site-packages \
LIGHTER_PUBLIC_KEY="..." \
LIGHTER_PRIVATE_KEY="..." \
LIGHTER_ACCOUNT_INDEX=XXXXX \
LIGHTER_API_KEY_INDEX=2 \
python adapters/lighter/scripts/lighter_tests/live_lighter_simple_place_cancel.py
```

This script:

- builds a `TradingNode` with:
  - `LighterDataClient` (public data),
  - `LighterExecutionClient` (private orders/account),
  - `LiveExecutionEngine` with reconciliation enabled;
- subscribes a single instrument (by default `BNBUSDC-PERP.LIGHTER`);
- places a small maker limit order away from the top of book (to avoid fills);
- cancels it after a short delay;
- logs order lifecycle events and reconciliation messages.

### 3. Live imbalance + TP strategy (BNB, 0.02, short‑biased test)

For a more realistic example with a strategy, use
`live_lighter_node_subscribe.py`. It runs an order‑book imbalance strategy
on BNB perpetuals, using only the adapter’s data and execution clients.

```bash
cd /root/myenv/lib/python3.12/site-packages
PYTHONPATH=/root/myenv/lib/python3.12/site-packages \
LIGHTER_PUBLIC_KEY="..." \
LIGHTER_PRIVATE_KEY="..." \
LIGHTER_ACCOUNT_INDEX=XXXXX \
LIGHTER_API_KEY_INDEX=2 \
LIGHTER_HTTP_RECORD=/tmp/lighter_http_live.jsonl \
LIGHTER_EXEC_RECORD=/tmp/lighter_exec_ws_live.jsonl \
LIGHTER_LIVE_DEMO_SECS=420 \
python adapters/lighter/scripts/lighter_tests/live_lighter_node_subscribe.py
```

Key properties of this strategy:

- Instrument: `BNBUSDC-PERP.LIGHTER` (mainnet market id 25).
- Direction: **long‑only** (BUY entries only).
- Size: fixed `0.02` BNB per entry (respecting tick sizes and min notional).
- Entry logic: subscribes order book deltas, computes notional bid/ask
  imbalance over the top levels, and enters when imbalance and its z‑score
  exceed configured thresholds.
- TP logic: once a `PositionOpened` or `PositionChanged` event arrives for
  this instrument, the strategy places a small‑profit maker take‑profit order
  (SELL 0.02 BNB, post‑only GTC) based on `avg_px_open * (1 + tp_pct)` and
  current best ask.
- Events: `OrderSubmitted`, `OrderAccepted`, `OrderFilled`,
  `PositionOpened`, `PositionChanged`, `PositionClosed` are all generated via
  `LighterExecutionClient` and reconciled by `LiveExecutionEngine`.
- Recording: HTTP and private WS JSONL can be analyzed offline using
  `analyze_http_jsonl.py` / `analyze_exec_ws_jsonl.py` /
  `analyze_private_ws_jsonl.py`.
---

## Architecture Overview

At a high level, the adapter consists of:

- **Instrument provider**
  - Loads `orderBooks` summaries and, on demand, `orderBookDetails`.
  - Synthesizes Nautilus `CryptoPerpetual` instruments.
  - Maintains a `market_index → InstrumentId` mapping used by data and
    execution clients.

- **Public data client (`LighterDataClient`)**
  - WebSocket subscriptions for:
    - `market_stats` → `CustomData(LighterMarketStats)`,
    - `order_book` → `OrderBookDeltas` (snapshot + deltas),
    - `trade` → `TradeTick`.
  - Uses strict `msgspec` schemas (`adapters/lighter/schemas/ws.py`) and
    forwards parsed objects into the Nautilus `DataEngine`.

- **Execution client (`LighterExecutionClient`)**
  - HTTP clients for account state and transactions:
    - balances and positions via `/api/v1/account`,
    - active orders via `/api/v1/accountActiveOrders`,
    - nonce management via `/api/v1/nextNonce`,
    - `sendTx` / `sendTxBatch` for signed transactions.
  - WebSocket client for private flows:
    - order/position/trade snapshots via `account_all/{account}` and split
      channels,
    - order submit/modify/cancel via `jsonapi/sendtx` and
      `jsonapi/sendtxbatch`.
  - Maps private WS messages into Nautilus events and reports:
    - order lifecycle events (`OrderSubmitted`, `OrderAccepted`, etc.),
    - `FillReport`/`OrderFilled` from account trades,
    - `PositionStatusReport` from position snapshots (NETTING).

- **Reconciliation**
  - At startup: HTTP snapshots provide an initial view of balances,
    positions, and open orders; these are emitted as reports and reconciled
    by `LiveExecutionEngine` via its mass‑status mechanism.
  - Periodic: `LiveExecutionEngine` calls
    `generate_order_status_reports(...)` and
    `generate_position_status_reports(...)` at configurable intervals,
    which the Lighter execution client implements using `/account` and
    `/accountActiveOrders`.
  - External orders (created from GUI or other tools) are detected from
    snapshots and mapped to synthetic `ClientOrderId`s with the `LIGHTER-EXT-`
    prefix, then reported as `OrderStatusReport` for reconciliation.

For low‑level details (exact schemas, enum mappings, NETTING behavior) see
the design documents below.

---

## Diagnostics and Tools

Some of the more commonly used tools under
`adapters/lighter/scripts/lighter_tests/`:

- Recording and analysis:
  - `record_public_ws.py` – capture public WS JSONL.
  - `record_private_ws.py` – capture private WS JSONL (account streams,
    with optional split channels and auth).
  - `record_http.py` – record HTTP account / active‑orders snapshots.
  - `analyze_http_jsonl.py`, `analyze_exec_ws_jsonl.py`,
    `analyze_private_ws_jsonl.py` – summarize recorded JSONL files.
  - `replay_private_ws.py` – replay private WS JSONL through the execution
    client to harden parsers.

- Execution / order lifecycle:
  - `exec_place_cancel_1pct.py` – place a maker order ~1% away from top of
    book, then cancel.
  - `exec_modify_cases.py` – targeted modify‑order tests.
  - `exec_batch_place_cancel.py` – batch place/cancel via
    `jsonapi/sendtxbatch` and Nautilus `SubmitOrderList`/`BatchCancelOrders`.

- Live engine integration:
  - `live_lighter_simple_place_cancel.py` – minimal round‑trip with a
    strategy under `TradingNode`.
  - `live_lighter_node_subscribe.py` – imbalance+TP strategy for BNB, with
    recording and periodic reconciliation enabled.

See `adapters/lighter/scripts/README.md` for a short description of the
script groups.

---

## Security Notes

- Keep `LIGHTER_PRIVATE_KEY` and any signer private keys out of source
  control.
- Do not print or log full private keys or auth tokens. The adapter and
  scripts log only high‑level status and error messages.
- If you create your own scripts or strategies, prefer reading credentials
  from environment variables or a separate secrets file with appropriate
  file‑system permissions.

---

## Further Reading

For readers who want to understand the adapter in depth, the following
documents provide more detail:

- `EVENT_AND_REPORT_HANDLING.md` – how private WS messages are mapped into
  Nautilus events and reports (orders, fills, positions).
- `RECONCILIATION_PERIODIC_DESIGN.md` – design and current behaviour of
  initial + periodic reconciliation with `LiveExecutionEngine`.
- `docs/ai_handover_lighter.md` (if present in your tree) – long‑form
  development notes and test logs used while hardening the adapter.

---

## Contributing

When modifying or extending the Lighter adapter:

- Follow the existing style of other Nautilus adapters:
  - strict `msgspec` schemas instead of loose dicts,
  - clear separation between transport logic and business logic,
  - minimal `getattr` / `hasattr` in core paths.
- Keep secrets out of code and documentation.
- Add or update small scripts under `adapters/lighter/scripts` to exercise
  new behaviors, and prefer using record/replay fixtures where possible.

If you intend to run the adapter in production, start with the provided
smoke tests and live examples, and then build your own strategies on top of
`LighterExecutionClient` and `LighterDataClient`.
