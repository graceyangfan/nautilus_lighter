#!/usr/bin/env python3
from __future__ import annotations

"""
Record Lighter public WS messages (market_stats/all, order_book/{id}, trade/{id}) to JSONL.

Usage:
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/record_public_ws.py \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --outfile /tmp/lighter_public.jsonl \
    --market-ids 0,1 \
    --seconds 60

Notes:
- This script only records inbound server messages. It does not require credentials.
- It subscribes to `market_stats/all` and to `order_book/{id}` + `trade/{id}`
  for each market id provided via `--market-ids`.
"""

import argparse
import asyncio
import json
from pathlib import Path
from typing import Iterable

import websockets


def _parse_ids(value: str | None) -> list[int]:
    if not value:
        return []
    out: list[int] = []
    for part in str(value).split(','):
        s = part.strip()
        if not s:
            continue
        try:
            out.append(int(s, 16) if s.startswith('0x') else int(s))
        except ValueError:
            continue
    return sorted(set(out))


async def _subscribe(ws, channels: Iterable[str]) -> None:
    for ch in channels:
        await ws.send(json.dumps({"type": "subscribe", "channel": ch}))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--ws', required=True, help='WebSocket endpoint, e.g. wss://.../stream')
    ap.add_argument('--outfile', required=True, help='Output JSONL filepath')
    ap.add_argument('--market-ids', default='0,1', help='Comma-separated market ids to record')
    ap.add_argument('--seconds', type=int, default=60, help='Runtime in seconds')
    args = ap.parse_args()

    mids = _parse_ids(args.market_ids)
    if not mids:
        print('No market ids provided; nothing to record')
        return 1

    out = Path(args.outfile)
    out.parent.mkdir(parents=True, exist_ok=True)

    channels = ["market_stats/all"]
    for mid in mids:
        channels += [f"order_book/{mid}", f"trade/{mid}"]

    async with websockets.connect(args.ws) as ws:
        # Wait handshake (server usually sends {"type":"connected"})
        try:
            await asyncio.wait_for(ws.recv(), timeout=5)
        except Exception:
            pass

        await _subscribe(ws, channels)

        end = asyncio.get_event_loop().time() + args.seconds
        with out.open('a', encoding='utf-8') as f:
            next_ping = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < end:
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
                obj = None
                try:
                    obj = json.loads(raw)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    t = str(obj.get('type') or '')
                    if t == 'ping':
                        try:
                            await ws.send(json.dumps({"type": "pong"}))
                        except Exception:
                            pass
                    ch = str(obj.get('channel') or '')
                    if (
                        ch == 'market_stats:all'
                        or ch.startswith('market_stats:')
                        or ch.startswith('order_book:')
                        or ch.startswith('trade:')
                    ):
                        rec = json.dumps({'ts': int(now*1000), 'msg': obj}, ensure_ascii=False)
                        f.write(rec + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))

