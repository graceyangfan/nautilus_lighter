#!/usr/bin/env python3
from __future__ import annotations

"""
BTC 交易脉冲：间隔性地产生小额下单，提升录到非空 trades 的概率。
不依赖 LighterTestContext，直接使用适配器 HTTP 客户端与 signer，避免导入链问题。
"""

import argparse
import asyncio
import logging
from decimal import Decimal
from pathlib import Path
import random

from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.http.account import LighterAccountHttpClient
from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient
from nautilus_trader.adapters.lighter.common.enums import LighterOrderType, LighterTimeInForce
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner


def configure_logging(verbose: bool = True) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s')


async def place_pulse(http_base: str, creds_path: Path, market_index: int, notional: float, mode: str, sell: bool) -> None:
    logger = logging.getLogger("btc_trade_pulse")
    pub = LighterPublicHttpClient(http_base)
    acc = LighterAccountHttpClient(http_base)
    txc = LighterTransactionHttpClient(http_base)
    creds = LighterCredentials.from_json_file(str(creds_path))
    signer = LighterSigner(creds, base_url=http_base, chain_id=300 if 'testnet' in http_base else 304)

    # 获取最优价与精度
    details = await pub.get_order_book_details(market_id=market_index)
    price_decimals = int(getattr(details, 'price_decimals', None) or getattr(details, 'supported_price_decimals', None) or 0)
    size_decimals = int(getattr(details, 'size_decimals', None) or 0)
    scale_p = 10 ** price_decimals
    scale_s = 10 ** size_decimals
    book = await pub.get_order_book_orders(market_id=market_index, limit=5)
    def _first(arr):
        if not isinstance(arr, list) or not arr: return None
        try: return float(str(arr[0].get('price')))
        except Exception: return None
    bid = _first(book.get('bids'))
    ask = _first(book.get('asks'))
    if bid is None and ask is None:
        logger.warning("no top-of-book; skip")
        return

    # 计算 base 数量（满足 notional）
    px = (bid if sell else (ask or bid)) or 0.0
    px_dec = Decimal(str(px))
    step = Decimal(10) ** Decimal(-(size_decimals or 0))
    base_dec = (Decimal(str(notional)) / (px_dec if px_dec > 0 else Decimal('1')))
    k = (base_dec / step).to_integral_value(rounding='ROUND_UP')
    base_dec = max(step, k * step)
    base_scaled = int((base_dec * Decimal(scale_s)).to_integral_value())

    # 价格与类型
    order_type = LighterOrderType.MARKET if mode.upper()=="MARKET" else LighterOrderType.LIMIT
    tif = LighterTimeInForce.IOC if order_type == LighterOrderType.MARKET else LighterTimeInForce.GTT
    # 以更安全的价格选择避免 21733 价格保护拒单：
    # 买单：price <= min(mark, bestAsk) * 1.02；卖单：price >= max(mark, bestBid) * 0.98
    mark = None
    try:
        # LighterOrderBookDetails 通常包含 last_trade_price 或 mark_price
        mp = getattr(details, 'mark_price', None) or getattr(details, 'last_trade_price', None)
        mark = float(str(mp)) if mp is not None else None
    except Exception:
        mark = None
    if order_type == LighterOrderType.MARKET:
        desired_px = (ask if not sell else bid) or (bid or ask or 0.0)
    else:
        if not sell:
            base_ref = min([x for x in [mark, ask] if x is not None]) if (mark is not None or ask is not None) else (ask or bid or 0.0)
            desired_px = float(base_ref) * 1.02
        else:
            base_ref = max([x for x in [mark, bid] if x is not None]) if (mark is not None or bid is not None) else (bid or ask or 0.0)
            desired_px = float(base_ref) * 0.98
    price_scaled = int(Decimal(str(desired_px)) * Decimal(scale_p))

    nonce = await acc.get_next_nonce(account_index=creds.account_index, api_key_index=creds.api_key_index)
    tx_info, err = await signer.sign_create_order_tx(
        market_index=market_index,
        client_order_index=random.randint(1<<20, 1<<28),
        base_amount=base_scaled,
        price=price_scaled,
        is_ask=sell,
        order_type=order_type,
        time_in_force=tif,
        reduce_only=False,
        trigger_price=0,
        order_expiry=(0 if tif==LighterTimeInForce.IOC else -1),
        nonce=nonce,
    )
    if err or not tx_info:
        logger.warning("sign error: %s", err)
        return
    resp = await txc.send_tx(tx_type=signer.get_tx_type_create_order(), tx_info=tx_info, auth=signer.create_auth_token())
    logger.info("pulse sendTx: code=%s tx_hash=%s", resp.get("code"), resp.get("tx_hash"))


async def run(http_base: str, creds_path: Path, market_index: int, notional: float, mode: str, pulses: int, interval: float) -> None:
    logger = logging.getLogger("btc_trade_pulse")
    for i in range(pulses):
        sell = (i % 2 == 1)
        try:
            await place_pulse(http_base, creds_path, market_index, notional, mode, sell)
        except Exception as e:
            logger.warning("pulse %s error: %s", i, e)
        await asyncio.sleep(interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC trade pulse to help capture trades")
    ap.add_argument('--http', default='https://testnet.zklighter.elliot.ai')
    ap.add_argument('--ws', default='wss://testnet.zklighter.elliot.ai/stream')
    ap.add_argument('--secrets', default='secrets/lighter_testnet_account.json')
    ap.add_argument('--market-index', type=int, default=1)
    ap.add_argument('--notional', type=float, default=6.0)
    ap.add_argument('--mode', default='MARKET', choices=['MARKET','LIMIT'])
    ap.add_argument('--pulses', type=int, default=8)
    ap.add_argument('--interval', type=float, default=60.0)
    ap.add_argument('--quiet', action='store_true')
    args=ap.parse_args()

    configure_logging(verbose=not args.quiet)
    asyncio.run(run(args.http, Path(args.secrets), args.market_index, args.notional, args.mode, args.pulses, args.interval))


if __name__=='__main__':
    main()
