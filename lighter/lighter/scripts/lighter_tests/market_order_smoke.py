#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

import sys
from pathlib import Path
# Ensure repo root on path so `scripts.*` is importable when run from /tmp
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from scripts.lighter_tests.context import LighterTestContext, configure_logging
from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce


async def run_once(context: LighterTestContext, market_index: int, notional_usd: float, side_sell: bool = False) -> None:
    logger = __import__("logging").getLogger("lighter_test.market_order")
    # 取最优价（已为缩放整型）
    bid, ask = await context.fetch_best_prices(market_index)
    if bid is None and ask is None:
        logger.warning("no best prices; using fallback 200000/201000")
        bid, ask = 200000, 201000
    price_scaled = bid if side_sell else (ask or bid or 200000)
    # 取 order_book 详情以估算精度与最小名义
    details = await context.http_public.get_order_book_details(market_id=market_index)
    price_decimals = int(details.get("price_decimals") or details.get("supported_price_decimals") or 0) if isinstance(details, dict) else 0
    size_decimals = int(details.get("size_decimals") or 0) if isinstance(details, dict) else 0
    # 估算 base 数量（满足最小名义）
    # notional_usd 以 USD 计，价格单位按 scale=10^price_decimals
    scale_p = 10 ** price_decimals
    px = Decimal(price_scaled) / Decimal(scale_p if scale_p > 0 else 1)
    min_size = Decimal(str(10)) ** Decimal(-(size_decimals or 0))
    if px <= 0:
        px = Decimal("2000")
    base = Decimal(str(notional_usd)) / px
    # 向上取到 size 步进
    step = min_size if min_size > 0 else Decimal("0.0001")
    k = (base / step).to_integral_value(rounding="ROUND_UP")
    base_actual = max(step, k * step)
    base_scaled = int((base_actual * (Decimal(10) ** Decimal(size_decimals))).to_integral_value())

    nonce = await context.fetch_next_nonce()
    tx, err = await context.signer.sign_create_order_tx(
        market_index=market_index,
        client_order_index=__import__("random").randint(1 << 20, 1 << 28),
        base_amount=int(base_scaled),
        price=int(price_scaled),             # 市价单使用“预期均价/滑点价格”
        is_ask=bool(side_sell),
        order_type=LighterOrderType.MARKET,
        time_in_force=LighterTimeInForce.IOC,
        reduce_only=False,
        trigger_price=0,
        order_expiry=0,
        nonce=nonce,
    )
    if err:
        raise RuntimeError(err)
    token = context.auth_token()
    resp = await context.http_tx.send_tx(
        tx_type=context.signer.get_tx_type_create_order(),
        tx_info=tx,
        auth=token,
    )
    logger.info("market order resp: %s", resp)


def main() -> None:
    ap = argparse.ArgumentParser(description="Place a MARKET order to generate trades")
    ap.add_argument("--http", default="https://testnet.zklighter.elliot.ai")
    ap.add_argument("--ws", default="wss://testnet.zklighter.elliot.ai/stream")
    ap.add_argument("--secrets", default="secrets/lighter_testnet_account.json")
    ap.add_argument("--market-index", type=int, default=0)
    ap.add_argument("--notional", type=float, default=20.0)
    ap.add_argument("--sell", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    configure_logging(verbose=not args.quiet)
    ctx = LighterTestContext(credentials_path=__import__("pathlib").Path(args.secrets), base_http=args.http, base_ws=args.ws)
    asyncio.run(run_once(ctx, args.market_index, args.notional, side_sell=args.sell))


if __name__ == "__main__":
    main()
