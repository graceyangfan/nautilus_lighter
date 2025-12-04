#!/usr/bin/env python3
from __future__ import annotations

"""
Small harness to exercise LighterExecutionClient.generate_*_reports on mainnet.

Behavior
--------
- Uses your mainnet credentials from env:
    LIGHTER_PUBLIC_KEY
    LIGHTER_PRIVATE_KEY
    LIGHTER_ACCOUNT_INDEX
    LIGHTER_API_KEY_INDEX
- Connects a LighterExecutionClient with BNB instruments loaded.
- Calls:
    - generate_position_status_reports (all instruments)
    - generate_order_status_reports (open-only, all markets with local activity)
- Prints the reports to stdout for inspection.
- Respects existing HTTP/WS recording via:
    LIGHTER_HTTP_RECORD
    LIGHTER_EXEC_RECORD

Usage
-----
  export LIGHTER_PUBLIC_KEY="..."
  export LIGHTER_PRIVATE_KEY="..."
  export LIGHTER_ACCOUNT_INDEX=XXXXX
  export LIGHTER_API_KEY_INDEX=2
  export LIGHTER_HTTP_RECORD=/tmp/lighter_http_periodic_recon.jsonl
  export LIGHTER_EXEC_RECORD=/tmp/lighter_exec_ws_periodic_recon.jsonl

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/test_periodic_reconciliation.py
"""

import asyncio
import os
from datetime import datetime, timezone

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import (
    GenerateOrderStatusReports,
    GeneratePositionStatusReports,
)
from nautilus_trader.model.identifiers import TraderId, StrategyId


async def main() -> int:
    http_url = os.getenv("LIGHTER_HTTP_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
    ws_url = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")

    pub = os.getenv("LIGHTER_PUBLIC_KEY")
    priv = os.getenv("LIGHTER_PRIVATE_KEY")
    acct = os.getenv("LIGHTER_ACCOUNT_INDEX")
    api_idx = os.getenv("LIGHTER_API_KEY_INDEX", "2")
    if not (pub and priv and acct):
        print("ERROR: missing LIGHTER_PUBLIC_KEY / LIGHTER_PRIVATE_KEY / LIGHTER_ACCOUNT_INDEX")
        return 1

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("LIGHTER-PERIODIC-TEST")
    strat = StrategyId("LIGHTER-PERIODIC-TEST")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    # Load BNB instruments to ensure mapping is ready
    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
    ]
    http_pub = LighterPublicHttpClient(base_url=http_url, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": ["BNB"]})
    inst_map = provider.get_all()
    if not inst_map:
        print("ERROR: no BNB instruments loaded")
        return 1
    # Use the first BNB instrument as our primary test instrument
    inst_id, inst = next(iter(inst_map.items()))
    for inst_obj in inst_map.values():
        cache.add_instrument(inst_obj)

    creds = LighterCredentials(
        pubkey=pub,
        account_index=int(acct),
        api_key_index=int(api_idx),
        private_key=priv,
    )
    exec_cfg = LighterExecClientConfig(
        base_url_http=http_url,
        base_url_ws=ws_url,
        credentials=creds,
        chain_id=304,  # mainnet
        subscribe_account_stats=False,
    )
    exec_client = LighterExecutionClient(
        loop=loop,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        base_url_http=http_url,
        base_url_ws=ws_url,
        credentials=creds,
        config=exec_cfg,
    )

    print("[PERIODIC] connecting execution client…")
    await exec_client._connect()  # type: ignore[protected-access]
    print("[PERIODIC] connected, running generate_* probes…")

    now = clock.timestamp_ns()
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    end = datetime.fromtimestamp(now / 1e9, tz=timezone.utc)

    # 1) PositionStatusReports snapshot
    pos_cmd = GeneratePositionStatusReports(
        instrument_id=inst_id,
        start=start,
        end=end,
        command_id=UUID4(),
        ts_init=now,
    )
    pos_reports = await exec_client.generate_position_status_reports(pos_cmd)
    print(f"[PERIODIC] PositionStatusReports count={len(pos_reports)}")
    for r in pos_reports:
        print(" POS", r.instrument_id, "side=", r.position_side, "qty=", r.quantity)

    # 2) OrderStatusReports snapshot (open-only, all markets with activity)
    ord_cmd = GenerateOrderStatusReports(
        instrument_id=inst_id,
        start=start,
        end=end,
        open_only=True,
        command_id=UUID4(),
        ts_init=now,
    )
    ord_reports = await exec_client.generate_order_status_reports(ord_cmd)
    print(f"[PERIODIC] OrderStatusReports count={len(ord_reports)}")
    for r in ord_reports:
        print(
            " ORD",
            r.instrument_id,
            "coid=",
            r.client_order_id,
            "voi=",
            r.venue_order_id,
            "status=",
            r.order_status,
        )

    await exec_client._disconnect()  # type: ignore[protected-access]
    print("[PERIODIC] done")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
