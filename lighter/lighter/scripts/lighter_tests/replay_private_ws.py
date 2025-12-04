#!/usr/bin/env python3
from __future__ import annotations

"""
Replay recorded private WS JSONL 文件，走 ExecutionClient 的解析链路做离线验证。

用法：
  source ~/nautilus_trader-develop/.venv/bin/activate
  cd /tmp
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python /root/nautilus_trader-develop/scripts/lighter_tests/replay_private_ws.py \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --base ETH \
    --jsonl /tmp/lighter_priv.jsonl
"""

import argparse
import asyncio
import json
from decimal import Decimal
from pathlib import Path

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.model.identifiers import TraderId, StrategyId


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--http', required=True)
    ap.add_argument('--ws', required=True)
    ap.add_argument('--secrets', required=True)
    ap.add_argument('--base', default='ETH')
    ap.add_argument('--jsonl', required=True)
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId('TRADER-LIGHTER-EXEC')
    strat = StrategyId('STRAT-REPLAY')
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookOrders", Quota.rate_per_minute(12)),
    ]
    http_pub = LighterPublicHttpClient(args.http, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={'bases':[args.base.strip().upper()]})
    if not provider.get_all():
        print('No instruments loaded; aborting')
        return 1

    creds = LighterCredentials.from_json_file(args.secrets)
    exec_cfg = LighterExecClientConfig(
        base_url_http=args.http,
        base_url_ws=args.ws,
        credentials=creds,
        chain_id=300 if 'testnet' in args.http else 304,
        subscribe_account_stats=False,
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

    counters = {'orders':0,'trades':0,'positions':0}
    orig_o = exec_client._process_account_orders
    orig_t = exec_client._process_account_trades
    orig_p = exec_client._process_account_positions
    def wrap_o(ods): counters['orders'] += len(ods or []); return orig_o(ods)
    def wrap_t(ts): counters['trades'] += len(ts or []); return orig_t(ts)
    def wrap_p(ps): counters['positions'] += len(ps or []); return orig_p(ps)
    exec_client._process_account_orders = wrap_o  # type: ignore
    exec_client._process_account_trades = wrap_t  # type: ignore
    exec_client._process_account_positions = wrap_p  # type: ignore

    # 逐行回放
    p = Path(args.jsonl)
    n=0
    with p.open('r', encoding='utf-8') as f:
        for line in f:
            line=line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            obj = rec.get('msg') if isinstance(rec, dict) else None
            if not isinstance(obj, dict):
                continue
            exec_client._on_ws_message_dict(obj)
            n+=1
    print('Replayed lines:', n)
    print('Counters:', counters)
    return 0

if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
