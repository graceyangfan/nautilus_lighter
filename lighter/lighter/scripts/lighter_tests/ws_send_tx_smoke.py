#!/usr/bin/env python3
from __future__ import annotations

"""
Send a signed tx via WS (jsonapi/sendtx) and print the WS response.

参考 lighter-python/examples/ws_send_tx.py，实现最小化的 WS 提交验证，
便于录制 jsonapi/sendtx(_result) 消息。
"""

import argparse
import asyncio
import json
import websockets

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', default='https://testnet.zklighter.elliot.ai')
    ap.add_argument('--ws', default='wss://testnet.zklighter.elliot.ai/stream')
    ap.add_argument('--secrets', default='secrets/lighter_testnet_account.json')
    ap.add_argument('--market-index', type=int, default=0)
    ap.add_argument('--notional', type=float, default=10.0)
    ap.add_argument('--sell', action='store_true')
    ap.add_argument('--outfile', default=None, help='可选：将 WS 回包追加写入该 JSONL 文件')
    args = ap.parse_args()

    creds = LighterCredentials.from_json_file(args.secrets)
    signer = LighterSigner(creds, base_url=args.http, chain_id=300 if 'testnet' in args.http else 304)
    pub = LighterPublicHttpClient(args.http)

    # derive decimals and best prices
    det = await pub.get_order_book_details(market_id=args.market_index)
    price_decimals = int(getattr(det, 'price_decimals', None) or getattr(det, 'supported_price_decimals', None) or 0)
    size_decimals = int(getattr(det, 'size_decimals', None) or 0)
    ob = await pub.get_order_book_orders(market_id=args.market_index, limit=5)
    def first(arr):
        return (arr or [{}])[0].get('price') if isinstance(arr, list) and arr else None
    bid = first(ob.get('bids'))
    ask = first(ob.get('asks'))
    bid = float(str(bid)) if bid is not None else None
    ask = float(str(ask)) if ask is not None else None
    px = bid if args.sell else (ask or bid or 2000.0)
    # compute minimal base amount to satisfy notional
    from decimal import Decimal
    step = Decimal(10) ** Decimal(-(size_decimals or 0))
    base = Decimal(str(args.notional)) / Decimal(str(px))
    k = (base / step).to_integral_value(rounding='ROUND_UP')
    base_actual = max(step, k * step)
    price_scaled = int(Decimal(str(px)) * (Decimal(10) ** Decimal(price_decimals)))
    base_scaled = int(base_actual * (Decimal(10) ** Decimal(size_decimals)))

    # sign tx
    from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
    acc = LighterAccountHttpClient(args.http)
    nonce = await acc.get_next_nonce(account_index=creds.account_index, api_key_index=creds.api_key_index)
    tx_info, err = await signer.sign_create_order_tx(
        market_index=args.market_index,
        client_order_index=int(__import__('time').time()*1e6) % (1<<48),
        base_amount=base_scaled,
        price=price_scaled,
        is_ask=bool(args.sell),
        order_type=LighterOrderType.MARKET,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=False,
        trigger_price=0,
        order_expiry=0,
        nonce=nonce,
    )
    if err or not tx_info:
        print('sign error:', err)
        return 2

    async with websockets.connect(args.ws) as ws:
        hello = await ws.recv()
        print('hello:', hello)
        payload = {
            'type': 'jsonapi/sendtx',
            'data': {
                'id': f'smoke-{__import__("time").time_ns()}',
                'tx_type': signer.get_tx_type_create_order(),
                'tx_info': json.loads(tx_info),  # WS 需要 JSON 对象
            }
        }
        await ws.send(json.dumps(payload))
        resp = await ws.recv()
        print('resp:', resp)
        # 可选：将回包落盘（与录制器分开连接时，便于把 jsonapi 回包也写入 JSONL）
        if args.outfile:
            try:
                import time, os
                obj = json.loads(resp)
                line = json.dumps({'ts': int(time.time()*1000), 'msg': obj}, ensure_ascii=False)
                with open(os.path.expanduser(args.outfile), 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception as e:  # pragma: no cover - 辅助功能
                print('write outfile failed:', e)
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
