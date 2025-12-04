from __future__ import annotations

"""
Execution client smoke: submit + cancel + observe callbacks (testnet).

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.test_exec_place_cancel \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --base ETH \
    --runtime 45

Notes:
  - Places a far-away LIMIT order (should not fill), waits, then issues cancel-all.
  - Wraps internal handlers to print sendtx results and account_all deltas.
  - Focus is adapter plumbing; not a full engine test.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig, LighterRateLimitConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.execution.messages import SubmitOrder, CancelAllOrders
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.common.factories import OrderFactory


async def _best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[float | None, float | None]:
    try:
        doc = await http.get_order_book_orders(mid, limit=5)
        def first_px(arr: list[dict] | None) -> float | None:
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            if x is None:
                return None
            return float(x)
        return first_px(doc.get("bids")), first_px(doc.get("asks"))
    except Exception:
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--ws", default="wss://testnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", default="secrets/lighter_testnet_account.json")
    ap.add_argument("--base", default="ETH")
    ap.add_argument("--runtime", type=int, default=45)
    args = ap.parse_args()

    # Wiring
    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-EXEC")
    strat = StrategyId("STRAT-TEST")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    # Public HTTP and provider with cautious quotas
    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookOrders", Quota.rate_per_minute(12)),
    ]
    http_pub = LighterPublicHttpClient(base_url=args.http, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": [args.base.strip().upper()]})
    if not provider.get_all():
        print("No instruments loaded; aborting")
        return 1
    # Pick first instrument
    inst_id, inst = next(iter(provider.get_all().items()))
    mid = int(inst.info["market_id"])  # fail-fast
    print("Using instrument:", inst_id, "market_id=", mid)

    # Credentials and execution client config
    creds = LighterCredentials.from_json_file(args.secrets)
    # Keep exec config minimal to avoid custom objects in msgspec-encoded config
    exec_cfg = LighterExecClientConfig(base_url_http=args.http, base_url_ws=args.ws, credentials=creds, chain_id=300)

    # Build execution client
    exec_client = LighterExecutionClient(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        config=exec_cfg,
    )

    # Wrap internal handlers to observe results
    counters = {"sendtx_ok": 0, "sendtx_any": 0, "account_orders": 0}
    _orig_sendtx = exec_client._handle_sendtx_result
    _orig_accall = exec_client._handle_account_all

    def _wrap_sendtx(obj: dict[str, Any]) -> None:
        try:
            data = obj.get("data") if isinstance(obj, dict) else None
            ok = False
            if isinstance(data, dict):
                st = data.get("status")
                ok = (str(st).lower() in ("ok", "success", "accepted")) or (data.get("code") == 200)
            counters["sendtx_any"] += 1
            counters["sendtx_ok"] += int(bool(ok))
            print("[sendtx] ok=", ok, "keys=", list((data or {}).keys()))
        except Exception:
            pass
        _orig_sendtx(obj)

    def _wrap_accall(msg):
        try:
            counters["account_orders"] += len(msg.data.orders or [])
            print("[account_all] orders=", len(msg.data.orders or []), "trades=", len(msg.data.trades or []), "positions=", len(msg.data.positions or []))
        except Exception:
            pass
        _orig_accall(msg)

    exec_client._handle_sendtx_result = _wrap_sendtx  # type: ignore[method-assign]
    exec_client._handle_account_all = _wrap_accall  # type: ignore[method-assign]

    # Connect exec client
    # Directly await internal connect to ensure mapping ready in this script context
    print("[exec] connecting...")
    await exec_client._connect()  # type: ignore[attr-defined]
    print("[exec] connected")
    if exec_client._ws is not None:
        await exec_client._ws.wait_until_ready(10)

    # Prepare far-away LIMIT order (reduce fill chance)
    best_bid, best_ask = await _best_prices(http_pub, mid)
    if best_bid is None and best_ask is None:
        best_bid, best_ask = 2000.0, 2010.0 if args.base.upper() == "ETH" else (100000.0, 100100.0)
    side = OrderSide.BUY
    px = best_bid * 0.8 if best_bid else (best_ask * 0.5 if best_ask else 2000.0)
    qty = str(inst.min_quantity)
    # Ensure min quantity is positive and small
    try:
        qf = float(qty)
        if qf <= 0:
            qty = "0.001"
    except Exception:
        qty = "0.001"

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=side,
        quantity=Quantity.from_str(qty),
        price=Price.from_str(f"{px:.2f}"),
        time_in_force=TimeInForce.GTC,
    )

    # Submit
    try:
        mid_check = exec_client._get_market_index(str(inst_id))
        print("ExecClient market_index lookup:", mid_check)
    except Exception as e:
        print("lookup error:", e)
    print("Submitting order:", order)
    prepared = exec_client._prepare_order(order)
    if prepared is None:
        print("prepare_order failed; aborting")
        await exec_client._disconnect()
        return 2
    if not exec_client._signer.available():
        print("signer not available")
        await exec_client._disconnect()
        return 3
    nonce_val = await exec_client._next_nonce()
    tx_info, err = await exec_client._signer.sign_create_order_tx(
        market_index=prepared.market_index,
        client_order_index=prepared.client_order_index,
        base_amount=prepared.base_amount,
        price=prepared.price,
        is_ask=prepared.is_ask,
        order_type=prepared.order_type,
        time_in_force=prepared.time_in_force,
        reduce_only=prepared.reduce_only,
        trigger_price=prepared.trigger_price,
        order_expiry=prepared.order_expiry,
        nonce=nonce_val,
    )
    if err or not tx_info:
        print("sign_create_order_tx failed:", err)
        await exec_client._disconnect()
        return 4
    # Record minimal mappings and emit submitted event (parity with adapter path)
    cv = prepared.coid_value
    exec_client._client_order_index_by_coid[cv] = prepared.client_order_index
    exec_client._coid_by_client_order_index[prepared.client_order_index] = prepared.order.client_order_id
    exec_client._strategy_by_coid[cv] = prepared.strategy_id
    exec_client._instrument_by_coid[cv] = prepared.instrument_id
    exec_client.generate_order_submitted(
        strategy_id=order.strategy_id,
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        ts_event=clock.timestamp_ns(),
    )
    await exec_client._send_ws_tx(exec_client._signer.get_tx_type_create_order(), tx_info)
    # Observe for half runtime
    await asyncio.sleep(max(5, args.runtime // 2))
    # Cancel all to clean up
    print("Sending cancel-all...")
    nonce2 = await exec_client._next_nonce()
    from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
    tx_all, erra = await exec_client._signer.sign_cancel_all_orders_tx(
        time_in_force=LighterCancelAllTif.IMMEDIATE,
        time=0,
        nonce=nonce2,
    )
    if tx_all and not erra:
        await exec_client._send_ws_tx(exec_client._signer.get_tx_type_cancel_all(), tx_all)
    # Final observe window
    await asyncio.sleep(max(5, args.runtime // 2))
    await exec_client._disconnect()
    print("Counters:", counters)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
