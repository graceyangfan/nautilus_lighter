#!/usr/bin/env python3
from __future__ import annotations

"""
Probe account_all(trades) timeline around BUY/SELL to infer message logic.

What it does:
- Subscribes `account_all/{account}` and `account_all_trades/{account}`
- Waits a short pre-roll to capture "before" frames
- Sends a MARKET IOC BUY, then after a short delay sends a MARKET IOC SELL
- Captures all `trades` arrays, groups by trade_id, and prints a timeline with
  first-seen/last-seen timestamps, occurrence count, and any field changes across repeats.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/trade_timeline_probe.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --market-index 0 \
    --notional 20 \
    --preroll 8 \
    --runtime 90
"""

import argparse
import asyncio
import json
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any

import websockets

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce


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


def _flatten_trades(trades_obj: Any) -> list[dict]:
    if isinstance(trades_obj, list):
        return [x for x in trades_obj if isinstance(x, dict)]
    if isinstance(trades_obj, dict):
        seq: list[dict] = []
        for v in trades_obj.values():
            if isinstance(v, list):
                seq.extend([x for x in v if isinstance(x, dict)])
            elif isinstance(v, dict):
                seq.append(v)
        return seq
    return []


async def run(args) -> int:
    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(creds, base_url=args.http, chain_id=300 if 'testnet' in args.http else 304)
    http_acc = LighterAccountHttpClient(args.http)
    http_pub = LighterPublicHttpClient(args.http)

    # Best prices and scaling
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

    # Observations store
    timelines: dict[int, list[tuple[float, dict, str]]] = defaultdict(list)  # trade_id -> [(ts, dict, channel)]

    async def listener(pre_roll_secs: int) -> None:
        token = signer.create_auth_token()
        async with websockets.connect(args.ws) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=5)
            except Exception:
                pass
            subs = [
                {"type": "subscribe", "channel": f"account_all/{creds.account_index}", "auth": token},
                {"type": "subscribe", "channel": f"account_all_trades/{creds.account_index}", "auth": token},
            ]
            for s in subs:
                try:
                    await ws.send(json.dumps(s))
                except Exception:
                    pass
            # Pre-roll
            end_pre = asyncio.get_event_loop().time() + pre_roll_secs
            while asyncio.get_event_loop().time() < end_pre:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    await ws.send(json.dumps({"type": "ping"}))
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                payload = obj.get('data') if isinstance(obj.get('data'), dict) else obj
                trades = _flatten_trades(payload.get('trades')) if isinstance(payload, dict) else []
                ch = str(obj.get('channel') or '')
                for t in trades:
                    try:
                        tid = int(t.get('trade_id'))
                    except Exception:
                        continue
                    timelines[tid].append((time.time(), t, ch))

            # Send BUY and SELL while still listening
            # BUY
            nonce = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
            tx_buy, err = await signer.sign_create_order_tx(
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
            if tx_buy:
                await ws.send(json.dumps({"type":"jsonapi/sendtx","data":{"id":"probe-buy","tx_type": signer.get_tx_type_create_order(),"tx_info": json.loads(tx_buy)}}))
                try:
                    print("sendtx BUY:", await asyncio.wait_for(ws.recv(), timeout=10))
                except Exception:
                    pass

            # Keep listening and schedule SELL
            sell_at = asyncio.get_event_loop().time() + max(5, args.runtime//3)
            end = asyncio.get_event_loop().time() + args.runtime
            sell_sent = False
            while asyncio.get_event_loop().time() < end:
                # send SELL once
                if not sell_sent and asyncio.get_event_loop().time() >= sell_at:
                    try:
                        nonce2 = await http_acc.get_next_nonce(creds.account_index, creds.api_key_index)
                        tx_sell, err2 = await signer.sign_create_order_tx(
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
                        if tx_sell:
                            await ws.send(json.dumps({"type":"jsonapi/sendtx","data":{"id":"probe-sell","tx_type": signer.get_tx_type_create_order(),"tx_info": json.loads(tx_sell)}}))
                            try:
                                print("sendtx SELL:", await asyncio.wait_for(ws.recv(), timeout=10))
                            except Exception:
                                pass
                    except Exception:
                        pass
                    sell_sent = True

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    try:
                        await ws.send(json.dumps({"type":"ping"}))
                    except Exception:
                        pass
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                payload = obj.get('data') if isinstance(obj.get('data'), dict) else obj
                trades = _flatten_trades(payload.get('trades')) if isinstance(payload, dict) else []
                ch = str(obj.get('channel') or '')
                for t in trades:
                    try:
                        tid = int(t.get('trade_id'))
                    except Exception:
                        continue
                    timelines[tid].append((time.time(), t, ch))

    await listener(args.preroll)

    # Analyze timelines
    print("=== Trade timelines ===")
    for tid, events in sorted(timelines.items(), key=lambda kv: kv[0]):
        first_ts = events[0][0] if events else 0
        last_ts = events[-1][0] if events else 0
        print(f"trade_id={tid} count={len(events)} first={first_ts:.3f} last={last_ts:.3f}")
        # Compare field changes
        base = events[0][1] if events else {}
        for idx, (ts, obj, ch) in enumerate(events):
            # diff keys
            diffs = []
            for k in sorted(set(list(base.keys()) + list(obj.keys()))):
                if base.get(k) != obj.get(k):
                    diffs.append(k)
            print(f"  [{idx}] {ch} ts={ts:.3f} keys={list(obj.keys())} diffs={diffs}")

    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', default='https://testnet.zklighter.elliot.ai')
    ap.add_argument('--ws', default='wss://testnet.zklighter.elliot.ai/stream')
    ap.add_argument('--secrets', default='secrets/lighter_testnet_account.json')
    ap.add_argument('--market-index', type=int, default=0)
    ap.add_argument('--notional', type=float, default=20.0)
    ap.add_argument('--preroll', type=int, default=8)
    ap.add_argument('--runtime', type=int, default=90)
    args = ap.parse_args()

    raise SystemExit(asyncio.run(run(args)))


if __name__ == '__main__':
    main()

