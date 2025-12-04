#!/usr/bin/env python3
"""测试 account_stats 频道是否可用

测试流程：
1. 订阅 account_stats
2. 下单（价格偏离盘口 1%，避免成交）
3. 撤销订单
4. 录制所有消息
5. 检查是否收到 account_stats 更新

Usage:
    export LIGHTER_ACCOUNT_INDEX=XXXXX
    export LIGHTER_API_KEY_INDEX=2
    export LIGHTER_EXEC_RECORD=/tmp/account_stats_test.jsonl
    python3 test_account_stats.py
"""

import asyncio
import json
import os
import time
from decimal import Decimal

import msgspec

# 设置环境变量
ACCOUNT_INDEX = int(os.getenv("LIGHTER_ACCOUNT_INDEX", "XXXXX"))
API_KEY_INDEX = int(os.getenv("LIGHTER_API_KEY_INDEX", "2"))
RECORD_FILE = os.getenv("LIGHTER_EXEC_RECORD", "/tmp/account_stats_test.jsonl")

# Mainnet
WS_URL = "wss://api.lighter.xyz/v1/ws"
HTTP_BASE = "https://api.lighter.xyz"

# 测试市场：BNB (market_index=25)
TEST_MARKET = 25
TEST_SYMBOL = "BNB"


async def main():
    from nautilus_trader.adapters.lighter.common.auth import LighterAuthManager
    from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
    from nautilus_trader.adapters.lighter.http.base import LighterHttpClient
    from nautilus_trader.adapters.lighter.websocket.client import LighterWebSocketClient

    print(f"=== Testing account_stats on Mainnet ===")
    print(f"Account: {ACCOUNT_INDEX}")
    print(f"Market: {TEST_MARKET} ({TEST_SYMBOL})")
    print(f"Recording to: {RECORD_FILE}")
    print()

    # 初始化
    creds = LighterCredentials.from_env(api_key_index=API_KEY_INDEX, account_index=ACCOUNT_INDEX)
    auth = LighterAuthManager(creds)
    http_client = LighterHttpClient(base_url=HTTP_BASE, auth_manager=auth)

    # 录制文件
    record_file = open(RECORD_FILE, "w")

    def record_msg(msg: dict):
        """录制消息"""
        record = {
            "ts": time.time_ns(),
            "src": "test",
            "msg": msg,
        }
        record_file.write(json.dumps(record) + "\n")
        record_file.flush()

    # 1. 获取当前市场价格
    print("[1] Fetching market price...")
    market_stats_url = f"{HTTP_BASE}/api/v1/market-stats?market_id={TEST_MARKET}"
    async with http_client._session.get(market_stats_url) as resp:
        market_data = await resp.json()
        last_price = float(market_data.get("last_trade_price", "0"))
        print(f"    Last price: {last_price}")

    if last_price == 0:
        print("ERROR: Failed to get market price")
        return

    # 2. 计算偏离 1% 的价格（买单低于市场价 1%）
    order_price = last_price * 0.99
    order_size = 0.01  # 最小下单量
    print(f"[2] Order params: price={order_price:.4f} (1% below), size={order_size}")

    # 3. 连接 WebSocket
    print("[3] Connecting WebSocket...")
    ws_client = LighterWebSocketClient(
        base_url=WS_URL,
        handler=lambda msg: record_msg(msg),
        loop=asyncio.get_event_loop(),
    )
    await ws_client.connect()
    print("    Connected")

    # 4. 订阅频道
    print("[4] Subscribing to channels...")
    token = auth.token(horizon_secs=60)

    channels = [
        f"account_stats/{ACCOUNT_INDEX}",  # ✅ 测试目标
        f"account_all/{ACCOUNT_INDEX}",
        f"account_all_orders/{ACCOUNT_INDEX}",
        f"account_all_trades/{ACCOUNT_INDEX}",
        f"account_all_positions/{ACCOUNT_INDEX}",
    ]

    for channel in channels:
        try:
            await ws_client.subscribe(channel, auth=token)
            print(f"    ✅ Subscribed: {channel}")
        except Exception as e:
            print(f"    ❌ Failed: {channel} - {e}")

    # 等待订阅确认
    await asyncio.sleep(2)

    # 5. 下单
    print("[5] Placing order...")
    from nautilus_trader.adapters.lighter.http.transaction import LighterTransactionHttpClient

    tx_client = LighterTransactionHttpClient(base_url=HTTP_BASE, auth_manager=auth)

    try:
        # 构造订单参数
        order_params = {
            "market_index": TEST_MARKET,
            "size": str(order_size),
            "price": str(order_price),
            "is_ask": False,  # 买单
            "reduce_only": False,
            "time_in_force": "good-till-time",
            "order_expiry": int((time.time() + 300) * 1000),  # 5 分钟后过期
        }

        result = await tx_client.create_order(
            account_index=ACCOUNT_INDEX,
            **order_params,
        )

        order_index = result.get("order_index")
        print(f"    ✅ Order placed: {order_index}")

        # 等待订单消息
        print("[6] Waiting for order messages (5s)...")
        await asyncio.sleep(5)

        # 6. 撤销订单
        print("[7] Canceling order...")
        cancel_result = await tx_client.cancel_order(
            account_index=ACCOUNT_INDEX,
            order_index=order_index,
        )
        print(f"    ✅ Order canceled")

        # 等待撤销消息
        print("[8] Waiting for cancel messages (5s)...")
        await asyncio.sleep(5)

    except Exception as e:
        print(f"    ❌ Error: {e}")

    # 7. 断开连接
    print("[9] Disconnecting...")
    await ws_client.disconnect()
    record_file.close()

    # 8. 分析录制结果
    print()
    print("=== Analysis ===")
    analyze_recording(RECORD_FILE)


def analyze_recording(filepath: str):
    """分析录制的消息"""
    channels_seen = set()
    account_stats_count = 0
    account_all_count = 0
    order_count = 0
    trade_count = 0
    position_count = 0

    with open(filepath) as f:
        for line in f:
            try:
                rec = json.loads(line)
                msg = rec.get("msg", {})
                ch = msg.get("channel", "")

                if ch:
                    channels_seen.add(ch)

                if "account_stats" in ch:
                    account_stats_count += 1
                    print(f"✅ Found account_stats message:")
                    print(f"   {json.dumps(msg, indent=2)[:500]}")

                if "account_all:" in ch:
                    account_all_count += 1

                if "account_all_orders" in ch:
                    order_count += 1

                if "account_all_trades" in ch:
                    trade_count += 1

                if "account_all_positions" in ch:
                    position_count += 1

            except:
                pass

    print(f"\nChannels seen: {len(channels_seen)}")
    for ch in sorted(channels_seen):
        print(f"  - {ch}")

    print(f"\nMessage counts:")
    print(f"  account_stats: {account_stats_count}")
    print(f"  account_all: {account_all_count}")
    print(f"  account_all_orders: {order_count}")
    print(f"  account_all_trades: {trade_count}")
    print(f"  account_all_positions: {position_count}")

    if account_stats_count > 0:
        print(f"\n✅ SUCCESS: account_stats is available!")
    else:
        print(f"\n❌ FAILED: account_stats not received")
        print(f"   Possible reasons:")
        print(f"   1. Channel not available on mainnet")
        print(f"   2. Subscription failed")
        print(f"   3. No balance change during test")


if __name__ == "__main__":
    asyncio.run(main())
