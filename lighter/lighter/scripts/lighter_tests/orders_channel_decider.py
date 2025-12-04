#!/usr/bin/env python3
from __future__ import annotations

"""
Decide whether `account_all` alone is sufficient or if `account_all_orders`
is required/available for order callbacks on current deployment.

What it does:
1) Subscribe to account_all, account_all_orders, account_all_trades, account_all_positions.
2) Place a non-marketable LIMIT order (GTT) via REST sendTx, then wait.
3) Optionally cancel-all and wait again.
4) Summarize whether orders updates were seen in either channel.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python scripts/lighter_tests/orders_channel_decider.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets secrets/lighter_testnet_account.json \
    --market-index 1 \
    --notional 15 \
    --runtime 90
"""

import argparse
import asyncio
import json
from decimal import Decimal
from typing import Any, Tuple

import websockets
import urllib.request
import urllib.parse
import importlib.util as _import_util
import sys as _sys
from pathlib import Path


def _first_price(arr: list[dict] | None) -> Decimal | None:
    if not isinstance(arr, list) or not arr:
        return None
    x = arr[0].get("price")
    if x is None:
        return None
    return Decimal(str(x))


def _http_get_json(url: str, headers: dict[str, str] | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _http_post_form(url: str, form: dict[str, str], headers: dict[str, str] | None = None) -> dict:
    data = urllib.parse.urlencode(form).encode('utf-8')
    hdrs = {'Content-Type': 'application/x-www-form-urlencoded'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode('utf-8'))


async def prepare_limit_payload(base_http: str, account_index: int, api_key_index: int, token: str | None, signer, mid: int, notional: Decimal, side_sell: bool = False) -> tuple[dict, str]:
    det = _http_get_json(f"{base_http}/api/v1/orderBookDetails?market_id={mid}")
    price_decimals = int(det.get('price_decimals') or det.get('supported_price_decimals') or 0)
    size_decimals = int(det.get('size_decimals') or 0)
    ob = _http_get_json(f"{base_http}/api/v1/orderBookOrders?market_id={mid}&limit=5")
    bid = _first_price(ob.get('bids'))
    ask = _first_price(ob.get('asks'))
    mark_price = det.get('mark_price') or det.get('last_trade_price')
    try:
        mark = Decimal(str(mark_price)) if mark_price is not None else None
    except Exception:
        mark = None
    print('[DECIDE][DBG] decimals price=', price_decimals, ' size=', size_decimals)
    print('[DECIDE][DBG] best bid=', bid, ' ask=', ask, ' mark=', mark)
    tick = Decimal(10) ** Decimal(-price_decimals)
    # choose a non-marketable price
    if side_sell:
        base = ask or bid or Decimal('2000')
        target = base + tick * 25
        if bid is not None:
            target = max(target, bid + tick * 30)
    else:
        base = bid or ask or Decimal('2000')
        target = base - tick * 25
        if ask is not None:
            target = min(target, ask - tick * 30)
    if target <= 0:
        target = Decimal('2000')

    price_scaled = int((target * (Decimal(10) ** Decimal(price_decimals))).to_integral_value())
    step = Decimal(10) ** Decimal(-(size_decimals or 0))
    px = target if target > 0 else Decimal('2000')
    base = (notional / px)
    k = (base / step).to_integral_value(rounding='ROUND_UP')
    base_actual = max(step, k * step)
    base_scaled = int((base_actual * (Decimal(10) ** Decimal(size_decimals))).to_integral_value())
    print('[DECIDE][DBG] chosen price=', target, ' price_scaled=', price_scaled, ' base_actual=', base_actual, ' base_scaled=', base_scaled)

    # next nonce
    hn = f"{base_http}/api/v1/nextNonce?account_index={account_index}&api_key_index={api_key_index}"
    headers = {'Authorization': token} if token else None
    nresp = _http_get_json(hn, headers=headers)
    nonce = int(nresp.get('next_nonce') or nresp.get('nonce') or 0)
    print('[DECIDE][DBG] nonce=', nonce, ' token_len=', len(token or ''))
    tx_info, err = await signer.sign_create_order_tx(
        market_index=mid,
        client_order_index=int(asyncio.get_event_loop().time()*1e9) % (1<<48),
        base_amount=base_scaled,
        price=price_scaled,
        is_ask=bool(side_sell),
        order_type=0,  # LIMIT
        time_in_force=1,  # GTT
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=nonce,
    )
    if err or not tx_info:
        raise RuntimeError(f"sign_create_order_tx error: {err}")
    payload = {
        'type': 'jsonapi/sendtx',
        'data': {
            'id': f'decide-{int(asyncio.get_event_loop().time()*1e9)}',
            'tx_type': signer.get_tx_type_create_order(),
            'tx_info': json.loads(tx_info),  # WS expects JSON object
        }
    }
    return payload, tx_info


async def decide(http: str, ws_url: str, secrets: str, market_index: int, notional: Decimal, runtime: int) -> int:
    # Use lighter-python SignerClient to avoid adapter import side-effects
    import json as _json
    data = _json.loads(Path(secrets).read_text(encoding='utf-8'))
    priv = str(data.get('private_key') or '')
    account_index = int(str(data.get('account_index')).replace('0x',''), 16) if str(data.get('account_index','')).startswith('0x') else int(data.get('account_index'))
    api_key_index = int(str(data.get('api_key_index')).replace('0x',''), 16) if str(data.get('api_key_index','')).startswith('0x') else int(data.get('api_key_index'))
    from lighter.signer_client import SignerClient
    client = SignerClient(url=http, private_key=priv, account_index=account_index, api_key_index=api_key_index)
    token, _ = client.create_auth_token_with_expiry()

    # Prepare payload and also send via REST once as confirmation
    # Prepare the same payload for WS path and also submit via lighter-python create_order (REST)
    # Compute scaled price/size
    det = _http_get_json(f"{http}/api/v1/orderBookDetails?market_id={market_index}")
    price_decimals = int(det.get('price_decimals') or det.get('supported_price_decimals') or 0)
    size_decimals = int(det.get('size_decimals') or 0)
    ob = _http_get_json(f"{http}/api/v1/orderBookOrders?market_id={market_index}&limit=5")
    bid = _first_price(ob.get('bids'))
    ask = _first_price(ob.get('asks'))
    tick = Decimal(10) ** Decimal(-price_decimals)
    # Prefer near-ask price for BUY (non-marketable: ask - 1 tick)
    if ask is not None:
        target = ask - tick * 1
        # also ensure on bid side: >= bid + tick
        if bid is not None:
            target = max(target, bid + tick)
    else:
        base_px = bid or Decimal('2000')
        target = base_px - tick * 1
    mark_price = det.get('mark_price') or det.get('last_trade_price')
    try:
        mark = Decimal(str(mark_price)) if mark_price is not None else None
    except Exception:
        mark = None
    print('[DECIDE][DBG] pre-place bid=', bid, ' ask=', ask, ' mark=', mark, ' tick=', tick, ' target=', target)
    if target <= 0:
        target = Decimal('2000')
    price_scaled = int((target * (Decimal(10) ** Decimal(price_decimals))).to_integral_value())
    step = Decimal(10) ** Decimal(-(size_decimals or 0))
    px = target if target > 0 else Decimal('2000')
    base = (notional / px)
    k = (base / step).to_integral_value(rounding='ROUND_UP')
    base_actual = max(step, k * step)
    base_scaled = int((base_actual * (Decimal(10) ** Decimal(size_decimals))).to_integral_value())
    print('[DECIDE][DBG] scaled price=', price_scaled, ' base_scaled=', base_scaled)

    # Submit via lighter-python REST helper
    # Try a few price nudges to satisfy price-protection (21734) if needed
    attempts = [0, 1, 2, 3, 4]  # small nudge towards ask for BUY
    accepted = False
    for off in attempts:
        px_try = price_scaled + int(off)
        try:
            _, api_resp, err = await client.create_order(
                market_index=market_index,
                client_order_index=int(asyncio.get_event_loop().time()*1e9) % (1<<48),
                base_amount=base_scaled,
                price=px_try,
                is_ask=False,
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=False,
            )
            print('[DECIDE] create_order try price_scaled=', px_try, 'resp=', api_resp, 'err=', err)
            if api_resp and getattr(api_resp, 'code', None) == 200:
                accepted = True
                price_scaled = px_try
                break
        except Exception as e:
            print('[DECIDE] create_order error at price_scaled=', px_try, 'e=', e)
            continue

    # Prepare a WS send payload mirroring the order
    ws_payload = {
        'type': 'jsonapi/sendtx',
        'data': {
            'id': f'decide-{int(asyncio.get_event_loop().time()*1e9)}',
            'tx_type': 14,  # TX_TYPE_CREATE_ORDER
            'tx_info': {
                'MarketIndex': market_index,
                'ClientOrderIndex': int(asyncio.get_event_loop().time()*1e9) % (1<<48),
                'BaseAmount': base_scaled,
                'Price': price_scaled,
                'IsAsk': 0,
                'Type': 0,
                'TimeInForce': 1,
                'ReduceOnly': 0,
                'TriggerPrice': 0,
                'OrderExpiry': -1,
            },
        },
    }

    counters = {
        'all_frames': 0,
        'orders_frames': 0,
        'orders_nonempty': 0,
        'trades_frames': 0,
        'trades_nonempty': 0,
        'positions_frames': 0,
        'positions_nonempty': 0,
    }

    async with websockets.connect(ws_url) as ws:
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            pass
        subs = [
            {"type": "subscribe", "channel": f"account_all/{account_index}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_orders/{account_index}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_trades/{account_index}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_positions/{account_index}", "auth": token},
        ]
        for s in subs:
            await ws.send(json.dumps(s))

        # Also send WS jsonapi to maximize chances of seeing a WS result and orders mapping
        print('[DECIDE][DBG] sending WS jsonapi/sendtx …')
        await ws.send(json.dumps(ws_payload))

        end = asyncio.get_event_loop().time() + runtime
        while asyncio.get_event_loop().time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                # send keepalive
                await ws.send(json.dumps({"type": "ping"}))
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            # Respond to server pings
            if isinstance(obj, dict) and str(obj.get('type') or '') == 'ping':
                try:
                    await ws.send(json.dumps({'type': 'pong'}))
                except Exception:
                    pass
                continue
            ch = str(obj.get('channel') or '')
            t = str(obj.get('type') or '')
            if t.startswith('jsonapi/sendtx'):
                print('[DECIDE] WS', t, 'code=', obj.get('code'), 'obj=', json.dumps(obj)[:200])
                continue
            if ch.startswith('account_all:'):
                counters['all_frames'] += 1
                data = obj.get('data') if isinstance(obj.get('data'), dict) else obj
                if isinstance(data.get('orders'), list):
                    if len(data['orders']) > 0:
                        counters['orders_nonempty'] += 1
                elif isinstance(data.get('orders'), dict):
                    # dict-of-lists
                    size = sum(len(v) for v in data['orders'].values() if isinstance(v, list))
                    if size > 0:
                        counters['orders_nonempty'] += 1
                if isinstance(data.get('trades'), list):
                    counters['trades_frames'] += 1
                    if len(data['trades']) > 0:
                        counters['trades_nonempty'] += 1
                elif isinstance(data.get('trades'), dict):
                    counters['trades_frames'] += 1
                    nonempty = any(isinstance(v, list) and len(v) > 0 for v in data['trades'].values())
                    if nonempty:
                        counters['trades_nonempty'] += 1
                if isinstance(data.get('positions'), list):
                    counters['positions_frames'] += 1
                    if len(data['positions']) > 0:
                        counters['positions_nonempty'] += 1
                elif isinstance(data.get('positions'), dict):
                    counters['positions_frames'] += 1
                    nonempty = any(True for _ in data['positions'].keys())
                    if nonempty:
                        counters['positions_nonempty'] += 1
            elif ch.startswith('account_all_orders:'):
                counters['orders_frames'] += 1
                data = obj.get('data') if isinstance(obj.get('data'), dict) else obj
                if isinstance(data.get('orders'), list) and len(data['orders']) > 0:
                    counters['orders_nonempty'] += 1
                elif isinstance(data.get('orders'), dict):
                    size = sum(len(v) for v in data['orders'].values() if isinstance(v, list))
                    if size > 0:
                        counters['orders_nonempty'] += 1
            elif ch.startswith('account_all_trades:'):
                counters['trades_frames'] += 1
            elif ch.startswith('account_all_positions:'):
                counters['positions_frames'] += 1

    print('[DECIDE] summary:', counters)
    # Decision heuristic:
    # - If orders_nonempty > 0 or orders_frames > 0 → account_all_orders useful.
    # - Else if trades/positions present reliably in account_all → rely on account_all.
    if counters['orders_nonempty'] > 0:
        print('[DECIDE] USE: account_all_orders (orders observed)')
    else:
        print('[DECIDE] USE: account_all (orders not observed; rely on trades/positions)')
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', required=True)
    ap.add_argument('--ws', required=True)
    ap.add_argument('--secrets', required=True)
    ap.add_argument('--market-index', type=int, default=1)
    ap.add_argument('--notional', type=float, default=15.0)
    ap.add_argument('--runtime', type=int, default=90)
    args = ap.parse_args()
    rc = asyncio.run(decide(args.http, args.ws, args.secrets, args.market_index, Decimal(str(args.notional)), args.runtime))
    raise SystemExit(rc)


if __name__ == '__main__':
    main()
