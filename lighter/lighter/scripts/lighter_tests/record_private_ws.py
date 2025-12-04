#!/usr/bin/env python3
from __future__ import annotations

"""
Record Lighter private WS messages (account_all + optional split) to JSONL.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/record_private_ws.py \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --account 202 \
    --outfile /tmp/lighter_priv.jsonl \
    --with-auth   # 可选，同时尝试携带 auth

注：本脚本仅记录服务端推送的入站消息，不记录本地私钥。
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import websockets

def redact(obj: dict) -> dict:
    # 入站消息通常不含敏感信息，这里保守过滤可能的 auth 字段
    if isinstance(obj, dict):
        if 'auth' in obj:
            obj = dict(obj)
            obj['auth'] = '***'
    return obj

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ws', required=True)
    ap.add_argument('--account', type=int, required=True)
    ap.add_argument('--outfile', required=True)
    ap.add_argument('--with-auth', action='store_true')
    ap.add_argument('--stats', action='store_true', help='同时订阅 account_stats/{account}')
    ap.add_argument('--seconds', type=int, default=60)
    ap.add_argument('--http', default='https://testnet.zklighter.elliot.ai')
    ap.add_argument('--secrets', default='~/nautilus_trader-develop/secrets/lighter_testnet_account.json')
    ap.add_argument('--split', action='store_true', help='同时订阅 account_all_orders/account_all_positions/account_all_trades')
    args = ap.parse_args()

    out = Path(os.path.expanduser(args.outfile))
    out.parent.mkdir(parents=True, exist_ok=True)

    token = None
    if args.with_auth:
        # Load signer module directly from source to avoid package __init__ side-effects
        try:
            import importlib.util as _util, sys as _sys
            root = Path(__file__).resolve().parents[2]
            signer_path = root / 'nautilus_trader' / 'adapters' / 'lighter' / 'common' / 'signer.py'
            spec = _util.spec_from_file_location('nautilus_trader.adapters.lighter.common.signer', str(signer_path))
            if spec and spec.loader:
                mod = _util.module_from_spec(spec)
                _sys.modules['nautilus_trader.adapters.lighter.common.signer'] = mod
                spec.loader.exec_module(mod)
                LighterCredentials = getattr(mod, 'LighterCredentials')
                LighterSigner = getattr(mod, 'LighterSigner')
                creds = LighterCredentials.from_json_file(os.path.expanduser(args.secrets))
                signer = LighterSigner(creds, base_url=args.http, chain_id=300 if 'testnet' in args.http else 304)
                token = signer.create_auth_token()
        except Exception:
            token = None

    async with websockets.connect(args.ws) as ws:
        try:
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            hello = None
        channels = [f"account_all/{args.account}"]
        if args.stats:
            channels.append(f"account_stats/{args.account}")
        if args.split:
            channels += [
                f"account_all_orders/{args.account}",
                f"account_all_positions/{args.account}",
                f"account_all_trades/{args.account}",
            ]
        if token:
            for ch in channels:
                await ws.send(json.dumps({"type": "subscribe", "channel": ch, "auth": token}))
        else:
            for ch in channels:
                await ws.send(json.dumps({"type": "subscribe", "channel": ch}))

        end = asyncio.get_event_loop().time() + args.seconds
        with out.open('a', encoding='utf-8') as f:
            # periodic app-level ping (Lighter) to keep connection active
            next_ping = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < end:
                # send Lighter 'ping' every 10s
                now = asyncio.get_event_loop().time()
                if now >= next_ping:
                    try:
                        await ws.send(json.dumps({"type": "ping"}))
                    except Exception:
                        pass
                    next_ping = now + 10
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    continue
                # parse and handle app-level ping/pong
                obj = None
                try:
                    obj = json.loads(raw)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    t = str(obj.get('type') or '')
                    if t == 'ping':
                        # Respond to app-level ping with pong
                        try:
                            await ws.send(json.dumps({"type": "pong"}))
                        except Exception:
                            pass
                    ch = str(obj.get('channel') or '')
                    if (
                        ch.startswith('account_all:') or ch.startswith('account_all_orders:') or ch.startswith('account_all_positions:') or ch.startswith('account_all_trades:') or ch.startswith('account_stats:')
                        or t.startswith('jsonapi/sendtx') or t.startswith('jsonapi/sendtxbatch')
                    ):
                        rec = json.dumps({'ts': int(asyncio.get_event_loop().time()*1000), 'msg': redact(obj)}, ensure_ascii=False)
                        f.write(rec + '\n')
    return 0

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
