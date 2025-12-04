#!/usr/bin/env python3
from __future__ import annotations

"""
Dual-path test: (1) place order via ExecutionClient over WS, (2) concurrently
sniff private WS raw messages for account_all/account_all_orders/account_stats.

Also sends a REST sendTx of the same signed order to confirm acceptance (code=200)
without altering the WS-first behavior of ExecutionClient.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/dual_ws_place_and_sniff.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --base ETH \
    --notional 20 \
    --runtime 120
"""

import argparse
import asyncio
from decimal import Decimal
import json
from typing import Any

import websockets
import os

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.common.factories import OrderFactory


async def best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[Decimal | None, Decimal | None]:
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


async def ws_sniffer(ws_url: str, token: str, account_index: int, runtime: int) -> None:
    async with websockets.connect(ws_url) as ws:
        try:
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
            print("[sniff] hello:", hello[:200])
        except Exception:
            pass
        subs = [
            {"type": "subscribe", "channel": f"account_all/{account_index}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_orders/{account_index}", "auth": token},
            {"type": "subscribe", "channel": f"account_stats/{account_index}", "auth": token},
        ]
        for s in subs:
            await ws.send(json.dumps(s))
        end = asyncio.get_event_loop().time() + runtime
        while asyncio.get_event_loop().time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                # Print only small snippet to avoid flooding
                print("[sniff]", raw[:240])
            except asyncio.TimeoutError:
                continue


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--base", default="ETH")
    ap.add_argument("--notional", type=float, default=20.0)
    ap.add_argument("--runtime", type=int, default=120)
    ap.add_argument("--chain-id", type=int, default=300, help="chain id (300=testnet, 304=mainnet)")
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
    http_pub = LighterPublicHttpClient(args.http, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": [args.base.strip().upper()]})
    if not provider.get_all():
        print("No instruments loaded; aborting")
        return 1
    inst_id, inst = next(iter(provider.get_all().items()))
    mid = int(inst.info["market_id"])
    print("Using instrument:", inst_id, "market_id=", mid)

    creds = LighterCredentials.from_json_file(args.secrets)
    env_priv = os.getenv("LIGHTER_PRIVATE_KEY")
    if env_priv:
        creds.private_key = env_priv
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=int(args.chain_id),
        subscribe_account_stats=True,
        subscribe_split_account_channels=True,
        post_submit_http_reconcile=True,
        post_submit_poll_attempts=3,
        post_submit_poll_interval_ms=1500,
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

    # Prepare order at near-top price (non-marketable)
    bid, ask = await best_prices(http_pub, mid)
    tick = Decimal(str(inst.price_increment))
    if bid is None and ask is None:
        bid, ask = Decimal("2000"), Decimal("2010")
    px = bid if bid is not None else (ask or Decimal("2000"))
    if ask is not None:
        px = min(px + tick, ask - tick)

    qty = Decimal(str(inst.min_quantity))
    min_notional = Decimal(str(inst.min_notional.as_decimal()))
    need = (min_notional / (px if px > 0 else Decimal("1")))
    step = Decimal(str(inst.size_increment))
    k = (need / step).to_integral_value(rounding="ROUND_UP")
    qty = max(qty, k * step)

    of = OrderFactory(trader, strat, clock)
    order = of.limit(
        instrument_id=inst_id,
        order_side=OrderSide.BUY,
        quantity=Quantity.from_str(str(qty)),
        price=Price.from_str(str(px.quantize(tick)) if tick > 0 else str(px)),
        time_in_force=TimeInForce.GTC,
    )

    # Start sniffer
    from nautilus_trader.adapters.lighter.common.signer import LighterSigner
    signer = LighterSigner(creds, base_url=args.http, chain_id=int(args.chain_id))
    tok = signer.create_auth_token(deadline_seconds=600)
    sniff_task = asyncio.create_task(ws_sniffer(args.ws, tok, creds.account_index, args.runtime))

    # Connect and submit via ExecutionClient (WS)
    print("[exec] connecting...")
    await exec_client._connect()  # type: ignore[attr-defined]
    print("[exec] connected")
    submit_cmd = __import__("nautilus_trader.execution.messages", fromlist=["SubmitOrder"]).SubmitOrder(
        trader_id=trader, strategy_id=strat, position_id=None, order=order,
        command_id=__import__("nautilus_trader.core.uuid", fromlist=["UUID4"]).UUID4(), ts_init=clock.timestamp_ns(),
    )
    exec_client.submit_order(submit_cmd)

    # REST confirmation (same signed tx) without altering WS-first behavior
    # We re-sign here to obtain tx_info for REST sendTx as confirmation only.
    from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce
    from nautilus_trader.adapters.lighter.common.normalize import decimal_to_scaled_int
    price_i = int((px / tick).to_integral_value())  # price units in ticks
    price_scaled = int(px / (Decimal(10) ** (-inst.price_precision)))
    size_scaled = int(qty / (Decimal(10) ** (-inst.size_precision)))
    nonce_val = await exec_client._http_account.get_next_nonce(creds.account_index, creds.api_key_index)
    tx_info, err = await signer.sign_create_order_tx(
        market_index=mid,
        client_order_index=int(clock.timestamp_ns() & ((1 << 48)-1)),
        base_amount=size_scaled,
        price=price_scaled,
        is_ask=False,
        order_type=LighterOrderType.LIMIT,
        time_in_force=LighterTimeInForce.GTT,
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=nonce_val,
    )
    if err or not tx_info:
        print("[REST confirm] failed signing:", err)
    else:
        tx_http = LighterTransactionHttpClient(args.http)
        res = await tx_http.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_info, auth=tok)
        print("[REST confirm] sendTx(create) code=", res.get("code"), "tx_hash=", res.get("tx_hash"))

    await sniff_task
    await exec_client._disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
