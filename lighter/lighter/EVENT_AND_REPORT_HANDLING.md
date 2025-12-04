# Lighter Event & Report Handling

This document explains how the Lighter adapter parses private data, generates
Nautilus `Event`/`Report` objects, and how this aligns with the patterns used
by other venue adapters (e.g. Bybit, Binance, Hyperliquid).

The goal is to make the behaviour explicit and predictable for:

- New order / cancel / modify flows.
- Order fills and position updates.
- Account balance snapshots.
- Error and rejection handling (`OrderRejected` / `OrderModifyRejected`).

All references below are to code under `adapters/lighter/`.

---

## 1. Private Channels & HTTP Snapshots

Lighter exposes the following private data sources:

- WebSocket:
  - `account_all_orders/{account}` → order state updates.
  - `account_all_trades/{account}` → trade/fill updates.
  - `account_all_positions/{account}` → position snapshots.
  - `account_stats/{account}` (testnet) → balance snapshots (not used on mainnet).
  - `jsonapi/sendtx` / `jsonapi/sendtxbatch` → async transaction acks.
- HTTP:
  - `/api/v1/account?by=index&value=...` → full account overview (balances + positions).
  - `/api/v1/accountOrders?by=account_index&value=...` → open orders snapshot (currently 403 on mainnet).

The adapter centralizes parsing and event generation in:

- `execution.py:LighterExecutionClient`
  - `_process_account_orders`
  - `_process_account_trades`
  - `_process_account_positions`
  - `_handle_sendtx_result` / `_handle_sendtx_batch_result`
  - `_handle_account_stats`
  - `_fetch_and_emit_account_state_http`
  - `_fetch_and_emit_account_orders_http`

Parsing is based on strict msgspec structs:

- `schemas/ws.py:LighterWsAccountAllRootStrict`,
  `LighterAccountOrderStrict`, `LighterAccountTradeStrict`,
  `LighterAccountPositionStrict`, `LighterAccountStatsStrict`.
- `schemas/http.py:LighterOrderBooksResponse`,
  `LighterOrderBookDetailsResponse`, `LighterAccountStateStrict`,
  `LighterAccountOrdersResponseStrict`.

All WS account frames are normalized via:

- `schemas/ws.py:normalize_account_all_bytes` →
  `LighterWsAccountAllRootStrict` (no dynamic dict parsing at runtime).

---

## 2. Order State Machine (`account_all_orders`)

### 2.1 Status normalization

The venue-specific `status` field on orders is normalized via:

- `common/enums.py:LighterEnumParser.parse_order_status`:
  - Active states (`in-progress`, `pending`, `open`, `active`, `resting`,
    `new`, `accepted`) → `OrderStatus.ACCEPTED`.
  - Cancel variants (`canceled-*`, `cancelled-*`) → `OrderStatus.CANCELED`,
    except `canceled-expired` → `OrderStatus.EXPIRED`.
  - Filled / executed variants → `OrderStatus.FILLED`.
  - Reject markers (`*reject*`, `failed`, `invalid`) → `OrderStatus.REJECTED`.

This mirrors how other adapters (Bybit/Binance) map venue-specific statuses into
a small set of Nautilus statuses used by the Execution layer.

### 2.2 Event generation from `account_all_orders`

Entry point:

- `execution.py:LighterExecutionClient._process_account_orders`

Flow per `LighterAccountOrderStrict` record:

1. Resolve `InstrumentId` from `market_index`:
   - `_resolve_instrument_id` → `self._market_instrument` → `LighterInstrumentProvider.find_by_market_index`.
2. Resolve `ClientOrderId`:
   - `_resolve_client_order_id(order_index, client_order_index)` using:
     - `self._coid_by_client_order_index` and
     - `self._coid_by_venue_order_index`
     - fallback scanning `self._venue_order_id_by_coid`.
3. Resolve `StrategyId`:
   - `self._cache.strategy_id_for_order(coid)`.
   - If `None`, treat as external order and emit an `OrderStatusReport`:
     ```python
     report = od.parse_to_order_status_report(
         account_id=self.account_id,
         instrument_id=inst_id,
         client_order_id=coid,
         venue_order_id=VenueOrderId(str(od.order_index)),
         ts_init=now,
     )
     self._send_order_status_report(report)
     ```
   - This mirrors the pattern used by Bybit: unrecognized orders produce
     status reports but no lifecycle events.
4. For known orders, apply `parse_order_status` and dispatch:

   - `OrderStatus.ACCEPTED` → `_handle_order_accepted_transition(...)`
   - `OrderStatus.FILLED`:
     - Ensure an `OrderAccepted` exists (if not, emit one once), but do **not**
       emit `OrderFilled` here; that is driven by `account_all_trades`.
   - `OrderStatus.CANCELED` / `EXPIRED`:
     - Use `_canceled_coids` to ensure `OrderCanceled` is emitted once per `ClientOrderId`.
     - If we never saw an ACCEPTED frame (e.g. missed WS frame), `_register_venue_mapping`
       is called to backfill the mapping and `accepted_coids` is updated.
   - `OrderStatus.REJECTED`:
     - Emit `OrderRejected` with `reason=od.status` (venue status text).

This design is intentionally aligned with other adapters:

- WS order feeds are authoritative for lifecycle events (`Accepted`, `Updated`,
  `Canceled`).
- HTTP snapshots (see §5) are best-effort reconciliation tools, not the primary
  trigger for events.

### 2.3 Handling new vs modified orders (`OrderAccepted` vs `OrderUpdated`)

Logic is encapsulated in:

- `execution.py:LighterExecutionClient._handle_order_accepted_transition`

Behaviour:

- If the order has never been seen as accepted (`coid` not in `_accepted_coids`):
  - Register `venue_order_id` mapping.
  - Mark as accepted.
  - Emit:
    ```python
    generate_order_accepted(strategy_id, instrument_id, client_order_id, venue_order_id, ts_event)
    ```
- If the order is already accepted:
  - Compare incoming `price` / `remaining_base_amount` with the cached order:
    - If either changed, or local order status is `PENDING_UPDATE`, emit:
      ```python
      generate_order_updated(..., quantity=..., price=..., trigger_price=..., ts_event=...)
      ```
    - Otherwise, treat the frame as a no-op for event purposes.

This matches the semantics in other adapters: subsequent "active" states
with a changed price/size are interpreted as `OrderUpdated`.

### 2.4 Cancellations and expiries (`OrderCanceled`)

When `status_norm` is `CANCELED` or `EXPIRED`:

- `_canceled_coids` ensures `OrderCanceled` is emitted once per `ClientOrderId`.
- If we never saw an ACCEPTED frame:
  - `_register_venue_mapping` is called defensively.
  - `coid` is added to `_accepted_coids` to guarantee a prior accepted state
    exists for downstream components.
- Finally:
  ```python
  generate_order_canceled(strategy_id, instrument_id, client_order_id, ts_event)
  ```

This is consistent with other adapters where late cancellations may arrive
without a clean `Accepted` event (e.g. reconnect scenarios).

---

## 3. Trade Processing (`account_all_trades`)

Entry point:

- `execution.py:LighterExecutionClient._process_account_trades`

Per `LighterAccountTradeStrict`:

1. Deduplication:
   - `_is_duplicate_trade(trade_id)` uses an LRU set of trade ids to avoid
     emitting multiple fills for the same trade.
2. Order lookup:
   - `_find_our_order_id(trade)`:
     - Prefer `bid_id` / `ask_id` mapped via `self._coid_by_venue_order_index`.
     - Fall back to `bid_client_id` / `ask_client_id` + `_coid_by_client_order_index`
       and register `venue_order_id` mapping when possible.
3. Resolve `ClientOrderId`, `StrategyId`, `InstrumentId`, `Instrument`:
   - `self._coid_by_venue_order_index` → `self._cache.strategy_id_for_order(coid)`
     → `self._cache.instrument_id_for_order(coid)` → `_ensure_instrument_cached`.
4. Determine maker/taker + side:
   - `_determine_maker_taker(trade, our_order_id)`:
     - Uses `is_maker_ask` and whether our order id is the bid or ask id to infer:
       - `is_maker: bool`
       - `OrderSide.BUY` / `OrderSide.SELL`
5. Compute commission:
   - `_calculate_commission(trade, is_maker, quote_currency)`:
     - Simple fixed-rate calculation:
       - maker: 0.002% (0.00002)
       - taker: 0.02% (0.0002)
     - This may be refined if Lighter starts including explicit fee fields in
       `account_all_trades`.
6. Emit `FillReport`:
   ```python
   report = FillReport(
       account_id=self.account_id,
       instrument_id=inst_id,
       venue_order_id=VenueOrderId(our_order_id),
       trade_id=TradeId(str(tr.trade_id)),
       order_side=order_side,
       last_qty=inst.make_qty(tr.size),
       last_px=inst.make_price(tr.price),
       commission=commission,
       liquidity_side=LiquiditySide.MAKER if is_maker else LiquiditySide.TAKER,
       report_id=UUID4(),
       ts_event=now,
       ts_init=now,
       client_order_id=coid,
   )
   self._send_fill_report(report)
   ```
7. Emit `OrderFilled`:
   - The `order_type` is taken from the cached order (no longer hard-coded),
     and we do not attach a `venue_position_id` (NETTING mode):
     ```python
     order = self._cache.order(coid)
     order_type = order.order_type if order is not None else OrderType.LIMIT
     self.generate_order_filled(
         strategy_id=strat,
         instrument_id=inst_id,
         client_order_id=coid,
         venue_order_id=VenueOrderId(our_order_id),
         venue_position_id=None,
         trade_id=TradeId(str(tr.trade_id)),
         order_side=order_side,
         order_type=order_type,
         last_qty=inst.make_qty(tr.size),
         last_px=inst.make_price(tr.price),
         quote_currency=inst.quote_currency,
         commission=commission,
         liquidity_side=LiquiditySide.MAKER if is_maker else LiquiditySide.TAKER,
         ts_event=now,
         info=None,
     )
     ```

This mirrors other adapters:

- Fills are driven by explicit trade events, not by order status transitions.
- A single trade event yields both:
  - an `execution.reports.FillReport` for accounting; and
  - an `OrderFilled` event for strategy-level logic.

---

## 4. Position Processing (`account_all_positions`)

Entry point:

- `execution.py:LighterExecutionClient._process_account_positions`

Flow:

1. Resolve `InstrumentId` from `market_id`:
   - `self._market_instrument` → `LighterInstrumentProvider.find_by_market_index`.
2. Ensure instrument is in cache:
   - `_ensure_instrument_cached`.
3. Convert numeric fields:
   - `position` → signed `Decimal`.
   - `avg_entry_price` → `Decimal` if present.
4. Deduplicate:
   - `self._last_position_state[market_id] = (size, avg_price, ts_last)`.
   - If size & avg price unchanged, and within optional suppression window
     (`position_dedup_suppress_window_ms`), skip.
5. Emit `PositionStatusReport`:
   - When position is flat (`size == 0`):
     ```python
     report = PositionStatusReport.create_flat(
         account_id=self.account_id,
         instrument_id=inst_id,
         size_precision=inst.size_precision,
         ts_init=self._clock.timestamp_ns(),
     )
     ```
   - When non-flat:
     - `PositionSide.LONG` if size > 0, `SHORT` if size < 0.
     - `quantity` = abs(size) scaled via `inst.make_qty(...)`.
6. Update Lighter-specific position cache:
   - `self._positions[market_id] = { "position": ..., "position_value": ...,
     "initial_margin_fraction": ..., "unrealized_pnl": ... }`
   - Used by `_calculate_position_margin` / `_calculate_total_margin`.

This is in line with other derivatives adapters: positions are maintained as
coarse-grained snapshots, with de-duplication to avoid log spam and
unnecessary downstream processing.

---

## 5. Account State & Open Orders (HTTP)

Because `account_stats` WS is not available on mainnet, the adapter uses HTTP
for account snapshots.

### 5.1 Account state (`get_account_state_strict`)

Entry point:

- `execution.py:LighterExecutionClient._fetch_and_emit_account_state_http`

HTTP client:

- `http/account.py:LighterAccountHttpClient.get_account_state_strict`:
  - Normalizes `/api/v1/account` payload into `LighterAccountStateStrict`:
    - `available_balance` / `total_balance`:
      - Accepts `available_balance` / `availableBalance` / `available`.
      - Accepts `total_balance` / `totalBalance` / `total_asset_value` / `total`.
      - Coerces via `normalize.to_float`.
    - `positions`:
      - Accepts list of dicts with `market_id`/`market_index` and `position`/`size`.
      - Produces `list[LighterAccountPositionStrict]`.
  - Execution then emits:
    ```python
    AccountState(balances=[AccountBalance(...)] , margins=[], reported=True, ts_event=ts)
    ```
    and passes `state.positions` to `_process_account_positions`.

This matches other adapters: account state is a derived signal from HTTP and
positions WS feed; on Lighter mainnet we rely primarily on HTTP for balances.

### 5.2 Open orders reconciliation (optional)

Entry point:

- `execution.py:LighterExecutionClient._fetch_and_emit_account_orders_http`

HTTP client:

- `http/account.py:LighterAccountHttpClient.get_account_orders`:
  - Decodes `/api/v1/accountOrders` into `LighterAccountOrdersResponseStrict`.
  - Returns `list[LighterAccountOrderStrict]`.
  - Execution reuses `_process_account_orders` to integrate the snapshot.

This path is used for:

- Post-submit reconciliation when WS callbacks are delayed (testnet usage).
- Rebuilding state after reconnects (when the endpoint is available).

On current mainnet deployments, `/api/v1/accountOrders` may return 403; the
adapter treats this endpoint as optional and continues relying on WS feeds.

---

## 6. sendTx / sendTxBatch Results & Rejections

The `jsonapi/sendtx(_batch)` results are handled by:

- `_handle_sendtx_result`
- `_handle_sendtx_batch_result`

### 6.1 Success detection

- `_is_sendtx_ok(msg)` checks:
  - `data.status` in `{"ok", "success", "accepted"}` (case-insensitive), or
  - `data.code == 200` or top-level `msg.code == 200`, or
  - boolean `True` status.

### 6.2 Rejection mapping

When a sendTx result indicates failure, the adapter:

- Resolves `ClientOrderId` via `_sendtx_resolve_coid` using `order_index` and
  `client_order_index`.
- Uses `_last_tx_action_by_order_index` / `_last_tx_action_by_client_index`
  to determine whether the last action was:
  - `create`, `modify`, or `cancel`.
- Emits:
  - `OrderModifyRejected` if last action was `modify`.
  - `OrderRejected` otherwise (e.g. new order rejected).

This mirrors the pattern used by other adapters: sendTx errors are interpreted
in the context of the last action kind to preserve the distinction between
creation and modification failures.

### 6.3 Batch results (`jsonapi/sendtxbatch`)

- When the venue returns per-item results (`data.results`), they are treated
  as a sequence of individual sendTx results.
- When only a `tx_hash` array is present (as in current mainnet behaviour),
  the adapter synthesizes `status="success", code=200` results:
  - Used only to mark the batch as successful.
  - No per-order rejection mapping is possible in this case.

This behaviour matches official lighter-python examples and observed mainnet
responses (`ws_send_batch_tx.py` sample).

---

## 7. Comparison to Other Adapters

Across the lifecycle, the Lighter adapter follows the same high-level patterns
as other Nautilus venue adapters (Bybit, Binance, Hyperliquid):

- **Orders**:
  - WS order streams (`account_all_orders`) are the primary source for
    `OrderAccepted`, `OrderUpdated`, `OrderCanceled`, `OrderRejected`.
  - HTTP open orders are best-effort reconciliation, not primary triggers.
- **Trades / fills**:
  - WS trade streams (`account_all_trades`) drive `FillReport` and `OrderFilled`.
  - Each trade event yields exactly one fill and one order-filled event.
- **Positions**:
  - WS positions (`account_all_positions`) are processed into `PositionStatusReport`
    with deduplication.
  - HTTP account state is used to bootstrap or cross-check positions.
- **Account state**:
  - On venues with `account_stats` WS (e.g. some Bybit deployments), WS is primary.
  - On Lighter mainnet, we use `/api/v1/account` as primary for balances.
- **Rejections**:
  - sendTx errors are mapped to `OrderRejected` / `OrderModifyRejected` using
    the last action kind (create/modify/cancel), just like other adapters.

The main Lighter-specific differences are:

- A unified `account_all/...` WS stream in some environments versus split
  channels in others; the adapter normalizes both via strict structs.
- Status vocabulary (`in-progress`, `canceled-post-only`, etc.) handled via
  `LighterEnumParser`.
- Mainnet HTTP endpoints vary slightly in availability (`accountOrders` may
  return 403); the adapter treats these as optional aids rather than hard
  dependencies.

Overall, the event/report handling logic for Lighter is intentionally shaped to
match the expectations of the Nautilus core and the patterns established in
other venue adapters, while respecting the specifics of Lighter's WS/HTTP
protocol and mainnet behaviour observed in live recordings.
