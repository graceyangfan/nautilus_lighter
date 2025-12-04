#!/usr/bin/env python3
from __future__ import annotations

"""
ExecutionClient smoke test (place + cancel) to validate callbacks.

Runs against Lighter testnet using the adapter's LighterExecutionClient and
prints concise observations for sendtx results and account callbacks.

Usage (from outside repo root to avoid import shadowing):

  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/exec_client_smoke.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --base ETH \
    --runtime 40
"""

import argparse
import asyncio
from decimal import Decimal
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.common.factories import OrderFactory


async def _best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[Decimal | None, Decimal | None]:
    try:
        doc = await http.get_order_book_orders(mid, limit=5)
        def first_px(arr: list[dict] | None) -> Decimal | None:
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            if x is None:
                return None
            return Decimal(str(x))
        return first_px(doc.get("bids")), first_px(doc.get("asks"))
    except Exception:
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", default="https://mainnet.zklighter.elliot.ai")
    ap.add_argument("--ws", default="wss://mainnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", default="secrets/lighter_testnet_account.json")
    ap.add_argument("--base", default="BNB")
    ap.add_argument("--runtime", type=int, default=60)
    ap.add_argument("--notional", type=float, default=10.0, help="notional value in USDC")
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-EXEC")
    strat = StrategyId("STRAT-TEST")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

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
    inst_id, inst = next(iter(provider.get_all().items()))
    mid = int(inst.info["market_id"])
    print("Using instrument:", inst_id, "market_id=", mid)

    import os
    pubkey = os.getenv("LIGHTER_PUBLIC_KEY")
    private_key = os.getenv("LIGHTER_PRIVATE_KEY")
    account_index = os.getenv("LIGHTER_ACCOUNT_INDEX")
    if not all([pubkey, private_key, account_index]):
        print("ERROR: Missing env vars (LIGHTER_PUBLIC_KEY, LIGHTER_PRIVATE_KEY, LIGHTER_ACCOUNT_INDEX)")
        return 1
    creds = LighterCredentials(
        pubkey=pubkey,
        account_index=int(account_index),
        api_key_index=0,
        private_key=private_key,
    )
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=304,  # mainnet
        subscribe_account_stats=False,  # not available on mainnet
        subscribe_split_account_channels=True,
        use_python_ws_private=True,
    )
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

    counters = {"sendtx_ok": 0, "sendtx_any": 0, "account_all_msgs": 0, "account_orders_msgs": 0, "account_positions_msgs": 0}
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
            o = len(msg.data.orders or [])
            p = len(msg.data.positions or [])
            t = len(msg.data.trades or [])
            counters["account_all_msgs"] += 1
            counters["account_orders_msgs"] += int(o > 0)
            counters["account_positions_msgs"] += int(p > 0)
            print(f"[account_all] orders={o} trades={t} positions={p}")
        except Exception:
            pass
        _orig_accall(msg)

    exec_client._handle_sendtx_result = _wrap_sendtx  # type: ignore[method-assign]
    exec_client._handle_account_all = _wrap_accall    # type: ignore[method-assign]

    print("[exec] connecting...")
    await exec_client._connect()  # type: ignore[attr-defined]
    print("[exec] connected")
    if exec_client._ws is not None:
        await exec_client._ws.wait_until_ready(10)

    # Price selection: non-marketable buy near top of book
    best_bid, best_ask = await _best_prices(http_pub, mid)
    tick = Decimal(str(inst.price_increment))
    if best_bid is None and best_ask is None:
        best_bid, best_ask = Decimal("2000"), Decimal("2010")
    px = best_bid if best_bid is not None else (best_ask or Decimal("2000"))
    # One tick inside spread but below ask
    if best_ask is not None:
        px = min(px + tick, best_ask - tick)
    # Fixed quantity: 0.02 BNB
    qty_dec = Decimal("0.02")
    min_notional = Decimal(str(inst.min_notional.as_decimal()))
    notional = px * qty_dec
    print(f"Order params: {qty_dec} {args.base} @ {px} = {notional} USDC (min: {min_notional})")
    if notional < min_notional:
        print(f"ERROR: Notional {notional} < min {min_notional}")
        return 1

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty_dec)),
        price=Price.from_str(str(px.quantize(tick)) if tick > 0 else str(px)),
        time_in_force=TimeInForce.GTC,
        post_only=True,  # POST_ONLY to avoid accidental fills on mainnet
    )

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
    # Fetch fresh nonce directly from server to avoid local drift on testnet
    nonce_val = await exec_client._http_account.get_next_nonce(
        account_index=creds.account_index,
        api_key_index=creds.api_key_index,
    )
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

    # Local mapping and submitted event
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
    # Prefer REST sendTx for deterministic acceptance on testnet
    token = exec_client._signer.create_auth_token()
    resp = await exec_client._http_tx.send_tx(
        tx_type=exec_client._signer.get_tx_type_create_order(),
        tx_info=tx_info,
        auth=token,
    )
    print("[REST] sendTx(create) code=", resp.get("code"), "tx_hash=", resp.get("tx_hash"))

    await asyncio.sleep(max(5, args.runtime // 2))

    print("Sending cancel-all...")
    nonce2 = await exec_client._next_nonce()
    tx_all, erra = await exec_client._signer.sign_cancel_all_orders_tx(
        time_in_force=LighterCancelAllTif.IMMEDIATE,
        time=0,
        nonce=nonce2,
    )
    if tx_all and not erra:
        token2 = exec_client._signer.create_auth_token()
        resp2 = await exec_client._http_tx.send_tx(
            tx_type=exec_client._signer.get_tx_type_cancel_all(),
            tx_info=tx_all,
            auth=token2,
        )
        print("[REST] sendTx(cancel_all) code=", resp2.get("code"), "tx_hash=", resp2.get("tx_hash"))

    await asyncio.sleep(max(5, args.runtime // 2))
    # HTTP reconciliation snapshot for sanity
    try:
        tok = exec_client._signer.create_auth_token()
        overview = await exec_client._http_account.get_account_overview(
            account_index=creds.account_index,
            auth=tok,
        )
        positions = overview.get("positions") if isinstance(overview, dict) else None
        print("[HTTP] positions count:", len(positions or []))
    except Exception as e:
        print("[HTTP] overview fetch failed:", e)
    await exec_client._disconnect()
    print("Counters:", counters)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
