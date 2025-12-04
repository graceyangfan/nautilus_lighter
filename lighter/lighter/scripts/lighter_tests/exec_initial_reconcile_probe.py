#!/usr/bin/env python3
from __future__ import annotations

"""
Initial reconciliation probe for ``LighterExecutionClient`` on Lighter mainnet.

Behavior
--------
- Connects a LighterExecutionClient with your credentials.
- Loads instruments for a given base (default BNB) so market_id -> instrument_id
  mapping is available.
- Lets the client run for a short period so initial HTTP + private WS snapshots
  can be processed.
- Logs key execution events flowing through ``ExecEngine.process``:
    * Account state / reports
    * PositionStatusReport
    * OrderSubmitted / OrderAccepted / OrderUpdated / OrderCanceled /
      OrderRejected / OrderModifyRejected / OrderFilled
- Optionally records raw HTTP and private WS traffic when the respective env
  vars are set:
    * LIGHTER_HTTP_RECORD (JSONL, HTTP)
    * LIGHTER_EXEC_RECORD (JSONL, private WS)

Environment (mainnet defaults)
------------------------------
  LIGHTER_HTTP_URL       = https://mainnet.zklighter.elliot.ai
  LIGHTER_WS_URL         = wss://mainnet.zklighter.elliot.ai/stream
  LIGHTER_PUBLIC_KEY     = ...
  LIGHTER_PRIVATE_KEY    = ...
  LIGHTER_ACCOUNT_INDEX  = ...
  LIGHTER_API_KEY_INDEX  = 2
  LIGHTER_BASE           = BNB

  LIGHTER_HTTP_RECORD    = /tmp/lighter_http_initial_snapshot.jsonl  (optional)
  LIGHTER_EXEC_RECORD    = /tmp/lighter_exec_ws_initial_snapshot.jsonl (optional)

Usage example
-------------
  export LIGHTER_PUBLIC_KEY="..."
  export LIGHTER_PRIVATE_KEY="..."
  export LIGHTER_ACCOUNT_INDEX=XXXXX
  export LIGHTER_API_KEY_INDEX=2
  export LIGHTER_BASE=BNB
  export LIGHTER_HTTP_RECORD=/tmp/lighter_http_initial_snapshot.jsonl
  export LIGHTER_EXEC_RECORD=/tmp/lighter_exec_ws_initial_snapshot.jsonl

  PYTHONPATH=/root/myenv/lib/python3.12/site-packages \\
  python adapters/lighter/scripts/lighter_tests/exec_initial_reconcile_probe.py
"""

import asyncio
import os

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.nautilus_pyo3 import Quota
from nautilus_trader.model.identifiers import StrategyId, TraderId


async def main() -> int:
    http_url = os.getenv("LIGHTER_HTTP_URL", "https://mainnet.zklighter.elliot.ai").rstrip("/")
    ws_url = os.getenv("LIGHTER_WS_URL", "wss://mainnet.zklighter.elliot.ai/stream")
    base = os.getenv("LIGHTER_BASE", "BNB").strip().upper()

    pub = os.getenv("LIGHTER_PUBLIC_KEY")
    priv = os.getenv("LIGHTER_PRIVATE_KEY")
    acct = os.getenv("LIGHTER_ACCOUNT_INDEX")
    api_idx = os.getenv("LIGHTER_API_KEY_INDEX", "2")

    if not pub or not priv or not acct:
        print("ERROR: missing LIGHTER_PUBLIC_KEY / LIGHTER_PRIVATE_KEY / LIGHTER_ACCOUNT_INDEX")
        return 1

    try:
        acct_i = int(acct)
        api_i = int(api_idx)
    except ValueError:
        print("ERROR: invalid LIGHTER_ACCOUNT_INDEX or LIGHTER_API_KEY_INDEX")
        return 1

    # Provide default record paths if not already configured.
    os.environ.setdefault("LIGHTER_HTTP_RECORD", "/tmp/lighter_http_initial_snapshot.jsonl")
    os.environ.setdefault("LIGHTER_EXEC_RECORD", "/tmp/lighter_exec_ws_initial_snapshot.jsonl")

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("NAUT-INIT-RECON")
    strat = StrategyId("NAUT-INIT-RECON")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    # Public HTTP client + instrument metadata (for market_id -> instrument_id)
    quotas = [
        ("lighter:/api/v1/orderBooks", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookDetails", Quota.rate_per_minute(6)),
        ("lighter:/api/v1/orderBookOrders", Quota.rate_per_minute(12)),
    ]
    http_pub = LighterPublicHttpClient(base_url=http_url, ratelimiter_quotas=quotas)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": [base]})
    instruments = provider.get_all()
    if not instruments:
        print(f"ERROR: no instruments loaded for base={base}")
        return 1
    for inst in instruments.values():
        cache.add_instrument(inst)

    creds = LighterCredentials(
        pubkey=pub,
        account_index=acct_i,
        api_key_index=api_i,
        private_key=priv,
    )

    exec_cfg = LighterExecClientConfig(
        base_url_http=http_url,
        base_url_ws=ws_url,
        credentials=creds,
        chain_id=304,  # mainnet chain id
        subscribe_account_stats=False,  # not available on mainnet currently
        post_submit_http_reconcile=False,
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

    # Log key execution events and reports so we can confirm that initial
    # account / position / order snapshots are parsed.
    def _log_evt(source: str, evt: object) -> None:
        name = type(evt).__name__
        interesting = {
            "AccountState",
            "AccountStateReport",
            "OrderStatusReport",
            "FillReport",
            "PositionStatusReport",
            "OrderSubmitted",
            "OrderAccepted",
            "OrderUpdated",
            "OrderCanceled",
            "OrderRejected",
            "OrderModifyRejected",
            "OrderFilled",
        }
        if name not in interesting:
            return
        parts: list[str] = [name]
        for attr in (
            "instrument_id",
            "client_order_id",
            "venue_order_id",
            "order_status",
            "position_id",
            "position_side",
            "quantity",
            "avg_px_open",
            "reason",
        ):
            if hasattr(evt, attr):
                parts.append(f"{attr}={getattr(evt, attr)}")
        print(f"[EVT][{source}]", " ".join(parts))

    msgbus.register(
        endpoint="ExecEngine.process",
        handler=lambda evt: _log_evt("ExecEngine.process", evt),
    )
    msgbus.register(
        endpoint="ExecEngine.reconcile_execution_report",
        handler=lambda evt: _log_evt("ExecEngine.reconcile_execution_report", evt),
    )

    print("[INIT-RECON] connecting LighterExecutionClient…")
    await exec_client._connect()  # type: ignore[protected-access]
    print("[INIT-RECON] connected; waiting for initial HTTP + WS snapshots…")
    # Allow some time for HTTP baseline + private WS streams to deliver data.
    await asyncio.sleep(20.0)
    print("[INIT-RECON] disconnecting…")
    await exec_client._disconnect()  # type: ignore[protected-access]
    print("[INIT-RECON] done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
