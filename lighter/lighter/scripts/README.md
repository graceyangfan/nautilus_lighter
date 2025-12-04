# Lighter Adapter Scripts

This directory contains helper scripts used to exercise and validate the
Lighter adapter in isolation and together with the Nautilus live engine.
They are grouped by purpose rather than by test framework.

- Execution smoke and lifecycle
  - `exec_client_smoke.py` – basic REST/WS connectivity and a small place/cancel
    flow using `LighterExecutionClient` only (no official SDK).
  - `exec_place_cancel_1pct.py` – place a maker order ~1% from the book,
    wait briefly, then cancel; useful for checking OrderAccepted /
    OrderCanceled events without fills.
  - `exec_modify_cases.py` – targeted modify‑order scenarios (price / size /
    both) to validate `modify_order` and related signer parameters.
  - `exec_batch_place_cancel.py` – batch place and batch cancel via
    `SubmitOrderList` / `BatchCancelOrders` and `jsonapi/sendtxbatch`.

- Private/public WS record & replay
  - `record_private_ws.py` – subscribe `account_all` (and split private
    channels when enabled) and write raw JSONL frames to disk.
  - `record_public_ws.py` – subscribe `market_stats`, `order_book`, and
    `trade` for selected markets and record JSONL.
  - `analyze_private_ws_jsonl.py` / `analyze_http_jsonl.py` /
    `analyze_exec_ws_jsonl.py` – small utilities to summarize recorded
    JSONL files (counts, status codes, duplication).
  - `replay_private_ws.py` – feed recorded private WS frames through
    `LighterExecutionClient` parsing for offline validation.

- Live TradingNode examples
  - `live_lighter_simple_place_cancel.py` – minimal `TradingNode` wiring
    with a small strategy that subscribes data, places a test order, then
    cancels it; shows how `LiveExecutionEngine` drives reconciliation.
  - `live_lighter_node_subscribe.py` – imbalance‑based strategy for
    `BNBUSDC-PERP.LIGHTER` which places small maker entries (0.02 BNB) and
    attaches a micro take‑profit order once a position opens.

- HTTP, signer, and auth diagnostics
  - `lighter_http_signer_smoke.py` – verifies the signer library can be
    loaded, creates an auth token, and calls a few HTTP endpoints.
  - `record_http.py` – record `/api/v1/account`, `/api/v1/accountActiveOrders`
    and related endpoints for reconciliation analysis.
  - `pyo3_smoke.py` – exercise the Pyo3 HTTP client and signer together.

All scripts are intended to obtain credentials (public key, private key,
account index, API key index) from environment variables such as
`LIGHTER_PUBLIC_KEY` and `LIGHTER_PRIVATE_KEY`. Do not hard‑code secrets
into these files. For typical usage, run scripts with:

```bash
PYTHONPATH=/path/to/site-packages \
LIGHTER_PUBLIC_KEY="..." \
LIGHTER_PRIVATE_KEY="..." \
LIGHTER_ACCOUNT_INDEX=... \
LIGHTER_API_KEY_INDEX=... \
python adapters/lighter/scripts/lighter_tests/exec_client_smoke.py
```

