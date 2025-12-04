#!/usr/bin/env python3
from __future__ import annotations

"""
Subscribe account_all/{account} on testnet and place MARKET IOC orders to
check if trades in account_all are duplicated (stateful) or delta-like.

Outputs statistics about unique trade_ids and duplicates observed.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/trade_dup_check.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --market-index 0 \
    --notional 25 \
    --runtime 75
"""

import argparse
import asyncio
import json
from decimal import Decimal
from typing import Any

import websockets

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient


def _scale_values(det: dict, px: Decimal, notional: float) -> tuple[int, int]:
    price_decimals = int(det.get("price_decimals") or det.get("supported_price_decimals") or 0)
    size_decimals = int(det.get("size_decimals") or 0)
    step = Decimal(10) ** Decimal(-(size_decimals or 0))
    base = Decimal(str(notional)) / (px if px > 0 else Decimal("2000"))
    k = (base / step).to_integral_value(rounding="ROUND_UP")
    base_actual = max(step, k * step)
    price_scaled = int(px * (Decimal(10) ** Decimal(price_decimals)))
    base_scaled = int(base_actual * (Decimal(10) ** Decimal(size_decimals)))
    return price_scaled, base_scaled


async def run(args) -> int:
    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(creds, base_url=args.http, chain_id=300 if 'testnet' in args.http else 304)
    http_acc = LighterAccountHttpClient(args.http)
    http_pub = LighterPublicHttpClient(args.http)

    # Derive scaled price/size for MARKET IOC (use best bid/ask as anchor)
    det = await http_pub.get_order_book_details(args.market_index)
    det_dict = det if isinstance(det, dict) else {}
    ob = await http_pub.get_order_book_orders(args.market_index, limit=5)
    def _first(arr: list[dict] | None):
        if not isinstance(arr, list) or not arr:
            return None
        return arr[0].get('price')
    bid = _first(ob.get('bids'))
    ask = _first(ob.get('asks'))
    bid = Decimal(str(bid)) if bid is not None else None
    ask = Decimal(str(ask)) if ask is not None else None
    px_buy = ask or bid or Decimal("2000")
    px_sell = bid or ask or Decimal("2000")
    px_buy_scaled, base_scaled = _scale_values(det_dict, px_buy, args.notional)
    px_sell_scaled, base_scaled2 = _scale_values(det_dict, px_sell, args.notional)

    # Stats for duplicates
    seen_trades: dict[int, int] = {}
    frames = 0

    async def listener() -> None:
        nonlocal frames
        token = signer.create_auth_token()
        async with websockets.connect(args.ws) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except Exception:
                pass
            await ws.send(json.dumps({"type": "subscribe", "channel": f"account_all/{creds.account_index}", "auth": token}))
            # also subscribe split trades if available
            await ws.send(json.dumps({"type": "subscribe", "channel": f"account_all_trades/{creds.account_index}", "auth": token}))
            end = asyncio.get_event_loop().time() + args.runtime
            while asyncio.get_event_loop().time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                    except Exception:
                        pass
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                ch = str(obj.get('channel') or '')
                if not (ch.startswith('account_all:') or ch.startswith('account_all_trades:')):
                    continue
                frames += 1
                # pick payload from root or data
                payload = obj
                if 'data' in obj and isinstance(obj['data'], dict):
                    payload = obj['data']
                trades = payload.get('trades')
                if isinstance(trades, dict):
                    # dict-of-lists or dict-of-dicts; flatten
                    seq = []
                    for v in trades.values():
                        if isinstance(v, list):
                            seq.extend(v)
                        elif isinstance(v, dict):
                            seq.append(v)
                    trades = seq
                if isinstance(trades, list):
                    for t in trades:
                        try:
                            tid = int(t.get('trade_id'))
                        except Exception:
                            continue
                        seen_trades[tid] = seen_trades.get(tid, 0) + 1

    async def sender() -> None:
        # sign and send BUY then SELL via WS jsonapi
        async with websockets.connect(args.ws) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except Exception:
                pass
            # BUY IOC MARKET (with scaled price as anchor)
            nonce = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
            from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce
            tx, err = await signer.sign_create_order_tx(
                market_index=args.market_index,
                client_order_index=int(asyncio.get_event_loop().time()*1e9) % (1<<48),
                base_amount=int(base_scaled),
                price=int(px_buy_scaled),
                is_ask=False,
                order_type=LighterOrderType.MARKET,
                time_in_force=LighterTimeInForce.IOC,
                reduce_only=False,
                trigger_price=0,
                order_expiry=0,
                nonce=nonce,
            )
            if tx:
                await ws.send(json.dumps({"type":"jsonapi/sendtx","data":{"id":"dupcheck-buy","tx_type": signer.get_tx_type_create_order(),"tx_info": json.loads(tx)}}))
                try:
                    print("sendtx BUY:", await asyncio.wait_for(ws.recv(), timeout=10))
                except Exception:
                    pass
            await asyncio.sleep(5)
            # SELL IOC MARKET
            nonce2 = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
            tx2, err2 = await signer.sign_create_order_tx(
                market_index=args.market_index,
                client_order_index=int(asyncio.get_event_loop().time()*1e9) % (1<<48),
                base_amount=int(base_scaled2),
                price=int(px_sell_scaled),
                is_ask=True,
                order_type=LighterOrderType.MARKET,
                time_in_force=LighterTimeInForce.IOC,
                reduce_only=False,
                trigger_price=0,
                order_expiry=0,
                nonce=nonce2,
            )
            if tx2:
                await ws.send(json.dumps({"type":"jsonapi/sendtx","data":{"id":"dupcheck-sell","tx_type": signer.get_tx_type_create_order(),"tx_info": json.loads(tx2)}}))
                try:
                    print("sendtx SELL:", await asyncio.wait_for(ws.recv(), timeout=10))
                except Exception:
                    pass

    # Run concurrently
    await asyncio.gather(listener(), sender())
    # Print stats
    total = sum(seen_trades.values())
    unique = len(seen_trades)
    dups = sum(1 for c in seen_trades.values() if c > 1)
    max_rep = max(seen_trades.values()) if seen_trades else 0
    print("Frames:", frames, "total_trades_seen:", total, "unique_trade_ids:", unique, "dup_ids:", dups, "max_repeats:", max_rep)
    if dups:
        # print sample duplicates
        sample = [tid for tid,c in seen_trades.items() if c>1][:10]
        print("Duplicate trade_ids sample:", sample)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', default='https://testnet.zklighter.elliot.ai')
    ap.add_argument('--ws', default='wss://testnet.zklighter.elliot.ai/stream')
    ap.add_argument('--secrets', default='secrets/lighter_testnet_account.json')
    ap.add_argument('--market-index', type=int, default=0)
    ap.add_argument('--notional', type=float, default=25.0)
    ap.add_argument('--runtime', type=int, default=75)
    args = ap.parse_args()

    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()
