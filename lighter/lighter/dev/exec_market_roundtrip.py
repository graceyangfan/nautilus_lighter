from __future__ import annotations

"""
Run a MARKET IOC open + MARKET IOC reduce-only close using LighterExecutionClient on testnet,
and write detailed order/trade/position callbacks from account_all to a log file.

Usage:
  PYTHONPATH=~/nautilus_trader-develop/.venv/lib/python3.11/site-packages \
  python -m nautilus_trader.adapters.lighter.dev.exec_market_roundtrip \
    --secrets ~/nautilus_trader-develop/secrets/lighter_testnet_account.json \
    --http https://testnet.zklighter.elliot.ai \
    --ws wss://testnet.zklighter.elliot.ai/stream \
    --market-id 0 \
    --side long \
    --notional 10 \
    --cancel-delay 60 \
    --runtime 180
"""

import argparse
import asyncio
from typing import Any

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.core.uuid import UUID4
from nautilus_trader.execution.messages import SubmitOrder
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import TraderId, StrategyId, PositionId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.common.factories import OrderFactory


async def _best_prices(http: LighterPublicHttpClient, mid: int) -> tuple[float | None, float | None]:
    try:
        doc = await http.get_order_book_orders(mid, limit=5)
        def first_px(arr):
            if not isinstance(arr, list) or not arr:
                return None
            x = arr[0].get("price")
            return float(x) if x is not None else None
        return first_px(doc.get("bids")), first_px(doc.get("asks"))
    except Exception:
        return None, None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--http", required=True)
    ap.add_argument("--ws", required=True)
    ap.add_argument("--market-id", type=int, default=0)
    ap.add_argument("--side", choices=["long", "short"], default="long")
    ap.add_argument("--notional", type=float, default=10.0)
    ap.add_argument("--cancel-delay", type=int, default=60)
    ap.add_argument("--runtime", type=int, default=180)
    ap.add_argument("--log-file", type=str, default="/tmp/lighter_exec_market_roundtrip.log")
    args = ap.parse_args()

    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader = TraderId("TRADER-LIGHTER-EXEC")
    strat = StrategyId("STRAT-TEST")
    pos_id = PositionId("POS-LIGHTER-TEST")
    msgbus = MessageBus(trader, clock)
    cache = Cache()

    # Provider with cautious limits
    http_pub = LighterPublicHttpClient(args.http)
    http_acc = LighterAccountHttpClient(args.http)
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async()
    # Find instrument by market_id
    inst_id = None
    for iid, inst in provider.get_all().items():
        if int(inst.info["market_id"]) == int(args.market_id):
            inst_id = iid
            break
    if inst_id is None:
        print("No instrument matched market-id=", args.market_id)
        return 2

    # Exec client
    creds = LighterCredentials.from_json_file(args.secrets)
    exec_cfg = LighterExecClientConfig(base_url_http=args.http, base_url_ws=args.ws, credentials=creds, chain_id=300)
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

    # Simple logger
    log_path = args.log_file
    def _log(*parts: Any) -> None:
        try:
            import time
            line = " ".join(str(p) for p in parts)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception:
            pass

    _log("START market_roundtrip", f"market_id={args.market_id}", f"side={args.side}", f"notional={args.notional}")

    # Wrap sendtx_result and account_all handlers for detailed logging
    counters: dict[str, Any] = {"orders": 0, "trades": 0, "positions": 0}
    orig_accall = exec_client._handle_account_all
    orig_sendtx = exec_client._handle_sendtx_result
    def _wrap_accall(msg):
        try:
            data = msg.data
            osz = len(data.orders or [])
            tsz = len(data.trades or [])
            psz = len(data.positions or [])
            counters["orders"] += osz
            counters["trades"] += tsz
            counters["positions"] += psz
            _log(f"acc_all:+orders={osz} +trades={tsz} +pos={psz}")
            # Log key details (best-effort)
            for od in (data.orders or []):
                _log(
                    "order:",
                    f"oi={od.order_index}",
                    f"coi={od.client_order_index}",
                    f"mid={od.market_index}",
                    f"status={od.status}",
                    f"is_ask={od.is_ask}",
                    f"price={od.price}",
                    f"filled_size={od.filled_size}",
                )
            for tr in (data.trades or []):
                _log(
                    "trade:",
                    f"tid={tr.trade_id}",
                    f"oi={tr.order_index}",
                    f"px={tr.price}",
                    f"sz={tr.size}",
                    f"is_ask={tr.is_ask}",
                    f"is_maker={tr.is_maker}",
                )
            for ps in (data.positions or []):
                _log(
                    "position:",
                    f"mid={ps.market_index}",
                    f"size={ps.size}",
                    f"aep={ps.avg_entry_price}",
                )
        except Exception as exc:
            _log("acc_all parse error:", exc)
        orig_accall(msg)
    exec_client._handle_account_all = _wrap_accall  # type: ignore[method-assign]

    def _wrap_sendtx(obj: dict[str, Any]) -> None:
        try:
            data = obj.get("data") if isinstance(obj, dict) else None
            if isinstance(data, dict):
                _log("sendtx_result:", f"status={data.get('status')}", f"code={data.get('code')}", f"tx_hash={data.get('tx_hash')}", f"error={data.get('error')}")
            else:
                _log("sendtx_result: (unparsed)", str(obj)[:200])
        except Exception as exc:
            _log("sendtx_result parse error:", exc)
        orig_sendtx(obj)
    exec_client._handle_sendtx_result = _wrap_sendtx  # type: ignore[method-assign]

    # Connect
    await exec_client._connect()  # type: ignore[attr-defined]
    _log("connected: exec ws + provider ready")

    # Compute target quantity from notional and best price
    best_bid, best_ask = await _best_prices(http_pub, int(args.market_id))
    ref_px = (best_ask if args.side == "long" else best_bid) or 0.0
    # Use provider info for decimals
    inst = provider.get_all()[inst_id]
    size_dec = int(inst.size_precision)
    step = 10 ** (-size_dec)
    qty_est = max(float(inst.min_quantity), float(args.notional) / max(ref_px, 1e-9))
    import math
    qty = math.ceil(qty_est / step) * step
    _log("computed:", f"ref_px={ref_px}", f"qty={qty}", f"min_qty={float(inst.min_quantity)}")

    # Optional: log account overview snapshot before open
    try:
        token = exec_client._signer.create_auth_token()  # type: ignore[attr-defined]
        ov = await http_acc.get_account_overview_strict(account_index=creds.account_index, auth=token)
        _log("overview(before):", f"total={ov.total_balance}", f"available={ov.available_balance}", f"pos={len(ov.positions)}")
    except Exception as e:
        _log("overview(before) error:", e)

    # Build MARKET IOC open
    of = OrderFactory(trader, strat, clock)
    side = OrderSide.BUY if args.side == "long" else OrderSide.SELL
    order_open = of.market(
        instrument_id=inst_id,
        order_side=side,
        quantity=Quantity.from_str(f"{qty}"),
        time_in_force=TimeInForce.IOC,
        reduce_only=False,
    )
    cmd_open = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=pos_id,
        order=order_open,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    _log("open: submitting MARKET IOC", f"qty={qty}")
    await exec_client.submit_order(cmd_open)

    await asyncio.sleep(max(5, int(args.cancel_delay)))

    # Build MARKET IOC reduce-only close
    order_close = of.market(
        instrument_id=inst_id,
        order_side=(OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY),
        quantity=Quantity.from_str(f"{qty}"),
        time_in_force=TimeInForce.IOC,
        reduce_only=True,
    )
    cmd_close = SubmitOrder(
        trader_id=trader,
        strategy_id=strat,
        position_id=pos_id,
        order=order_close,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    _log("close: submitting MARKET IOC reduce_only", f"qty={qty}")
    await exec_client.submit_order(cmd_close)

    # Observe
    await asyncio.sleep(max(5, int(args.runtime)))
    # Optional: log account overview snapshot after close
    try:
        token2 = exec_client._signer.create_auth_token()  # type: ignore[attr-defined]
        ov2 = await http_acc.get_account_overview_strict(account_index=creds.account_index, auth=token2)
        _log("overview(after):", f"total={ov2.total_balance}", f"available={ov2.available_balance}", f"pos={len(ov2.positions)}")
    except Exception as e:
        _log("overview(after) error:", e)
    await exec_client._disconnect()  # type: ignore[attr-defined]
    _log("summary:", f"orders={counters['orders']}", f"trades={counters['trades']}", f"positions={counters['positions']}")
    print("Log written to:", log_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
