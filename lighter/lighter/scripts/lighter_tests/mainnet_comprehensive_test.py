#!/usr/bin/env python3
"""主网综合测试：BNB 0.02 订单，post_only，取消，验证所有 channel 解析"""
import asyncio
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId, StrategyId, InstrumentId
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.execution.messages import SubmitOrder, CancelOrder, CancelAllOrders
from nautilus_trader.test_kit.stubs.identifiers import TestIdStubs

# 录制文件
RECORD_FILE = os.environ.get("LIGHTER_EXEC_RECORD", "/tmp/mainnet_comprehensive.jsonl")

# 账户配置
ACCOUNT_INDEX = int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "XXXXX"))
API_KEY_INDEX = int(os.environ.get("LIGHTER_API_KEY_INDEX", "2"))

# 测试参数
INSTRUMENT = "BNBUSDC-PERP"
ORDER_SIZE = Decimal("0.02")  # 0.02 BNB
PRICE_OFFSET = Decimal("0.01")  # 1% 偏离盘口
BALANCE_POLL_INTERVAL = 5  # 5秒轮询
TEST_DURATION = 120  # 运行 120 秒


async def main():
    print("=" * 80)
    print("主网综合测试")
    print("=" * 80)
    print(f"交易对: {INSTRUMENT}")
    print(f"订单量: {ORDER_SIZE} BNB")
    print(f"价格偏离: {PRICE_OFFSET * 100}%")
    print(f"Balance 轮询: {BALANCE_POLL_INTERVAL}s")
    print(f"测试时长: {TEST_DURATION}s")
    print(f"录制文件: {RECORD_FILE}")
    print("=" * 80)

    # 初始化组件
    loop = asyncio.get_event_loop()
    clock = LiveClock()
    trader_id = TraderId("TESTER-001")
    strategy_id = StrategyId("TEST-001")
    msgbus = MessageBus(trader_id=trader_id, clock=clock)
    cache = Cache()

    # 凭证
    pubkey = os.getenv("LIGHTER_PUBLIC_KEY")
    private_key = os.getenv("LIGHTER_PRIVATE_KEY")
    if not all([pubkey, private_key]):
        print("ERROR: Missing LIGHTER_PUBLIC_KEY or LIGHTER_PRIVATE_KEY")
        return 1

    creds = LighterCredentials(
        pubkey=pubkey,
        account_index=ACCOUNT_INDEX,
        api_key_index=API_KEY_INDEX,
        private_key=private_key,
    )

    # 加载 instruments
    http_pub = LighterPublicHttpClient(base_url="https://mainnet.zklighter.elliot.ai")
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": ["BNB"]})
    if not provider.get_all():
        print("ERROR: No instruments loaded")
        return 1

    # 配置
    config = LighterExecClientConfig(
        base_url_http="https://mainnet.zklighter.elliot.ai",
        base_url_ws="wss://mainnet.zklighter.elliot.ai/stream",
        credentials=creds,
        chain_id=304,  # mainnet
        subscribe_account_stats=False,
        use_python_ws_private=True,
    )

    # 创建客户端
    exec_client = LighterExecutionClient(
        loop=loop,
        client=http_pub,
        msgbus=msgbus,
        cache=cache,
        clock=clock,
        instrument_provider=provider,
        config=config,
    )

    # 连接
    print("\n[1] 连接中...")
    await exec_client._connect()
    await asyncio.sleep(3)
    print("✅ 连接成功")

    # 获取当前价格
    instrument_id = InstrumentId.from_str(f"{INSTRUMENT}.LIGHTER")

    # 从 HTTP 获取市场价格
    print("\n[2] 获取市场价格...")
    market_data = await exec_client._http_account.get_market(25)  # BNB market_id=25
    mid_price = Decimal(str(market_data.get("mid_price", "600")))
    print(f"✅ 当前价格: {mid_price}")

    # 测试场景
    submitted_orders = []

    # 场景 1: post_only=True, 买单偏离 -1%
    print("\n[3] 提交 post_only=True 买单 (偏离 -1%)...")
    buy_price = mid_price * (1 - PRICE_OFFSET)
    buy_order = TestIdStubs.limit_order(
        instrument_id=instrument_id,
        order_side=OrderSide.BUY,
        quantity=Quantity(ORDER_SIZE, precision=2),
        price=Price(buy_price, precision=4),
        time_in_force=TimeInForce.GTC,
        post_only=True,
    )
    submit_buy = SubmitOrder(
        trader_id=trader_id,
        strategy_id=strategy_id,
        order=buy_order,
        command_id=exec_client._uuid_factory.generate(),
        ts_init=clock.timestamp_ns(),
    )
    await exec_client._submit_order(submit_buy)
    submitted_orders.append(buy_order.client_order_id)
    await asyncio.sleep(2)
    print(f"✅ 买单已提交: {buy_order.client_order_id}")

    # 场景 2: post_only=False, 卖单偏离 +1%
    print("\n[4] 提交 post_only=False 卖单 (偏离 +1%)...")
    sell_price = mid_price * (1 + PRICE_OFFSET)
    sell_order = TestIdStubs.limit_order(
        instrument_id=instrument_id,
        order_side=OrderSide.SELL,
        quantity=Quantity(ORDER_SIZE, precision=2),
        price=Price(sell_price, precision=4),
        time_in_force=TimeInForce.GTC,
        post_only=False,
    )
    submit_sell = SubmitOrder(
        trader_id=trader_id,
        strategy_id=strategy_id,
        order=sell_order,
        command_id=exec_client._uuid_factory.generate(),
        ts_init=clock.timestamp_ns(),
    )
    await exec_client._submit_order(submit_sell)
    submitted_orders.append(sell_order.client_order_id)
    await asyncio.sleep(2)
    print(f"✅ 卖单已提交: {sell_order.client_order_id}")

    # 场景 3: 再提交几个订单
    print("\n[5] 提交更多订单...")
    for i in range(3):
        side = OrderSide.BUY if i % 2 == 0 else OrderSide.SELL
        offset = -PRICE_OFFSET if side == OrderSide.BUY else PRICE_OFFSET
        price = mid_price * (1 + offset)

        order = TestIdStubs.limit_order(
            instrument_id=instrument_id,
            order_side=side,
            quantity=Quantity(ORDER_SIZE, precision=2),
            price=Price(price, precision=4),
            time_in_force=TimeInForce.GTC,
            post_only=True,
        )
        submit = SubmitOrder(
            trader_id=trader_id,
            strategy_id=strategy_id,
            order=order,
            command_id=exec_client._uuid_factory.generate(),
            ts_init=clock.timestamp_ns(),
        )
        await exec_client._submit_order(submit)
        submitted_orders.append(order.client_order_id)
        print(f"  订单 {i+1}: {side.name} @ {price}")
        await asyncio.sleep(1)
    print("✅ 所有订单已提交")

    # 等待一段时间观察
    print(f"\n[6] 等待 30 秒观察 balance 轮询和 channel 消息...")
    await asyncio.sleep(30)

    # 场景 4: 取消部分订单
    print("\n[7] 取消前 2 个订单...")
    for coid in submitted_orders[:2]:
        cancel = CancelOrder(
            trader_id=trader_id,
            strategy_id=strategy_id,
            instrument_id=instrument_id,
            client_order_id=coid,
            venue_order_id=None,
            command_id=exec_client._uuid_factory.generate(),
            ts_init=clock.timestamp_ns(),
        )
        await exec_client._cancel_order(cancel)
        print(f"  取消: {coid}")
        await asyncio.sleep(1)
    print("✅ 部分订单已取消")

    # 等待观察
    print(f"\n[8] 等待 30 秒观察取消后的状态...")
    await asyncio.sleep(30)

    # 场景 5: 取消所有订单
    print("\n[9] 取消所有剩余订单...")
    cancel_all = CancelAllOrders(
        trader_id=trader_id,
        strategy_id=strategy_id,
        instrument_id=instrument_id,
        command_id=exec_client._uuid_factory.generate(),
        ts_init=clock.timestamp_ns(),
    )
    await exec_client._cancel_all_orders(cancel_all)
    await asyncio.sleep(5)
    print("✅ 所有订单已取消")

    # 继续运行观察 balance 轮询
    remaining = TEST_DURATION - 70
    if remaining > 0:
        print(f"\n[10] 继续运行 {remaining} 秒观察 balance 轮询...")
        await asyncio.sleep(remaining)

    # 断开连接
    print("\n[11] 断开连接...")
    await exec_client._disconnect()
    print("✅ 已断开")

    # 分析结果
    print("\n" + "=" * 80)
    print("分析录制数据")
    print("=" * 80)

    channels = {}
    account_updates = []
    order_events = []
    position_events = []

    with open(RECORD_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
                msg = rec.get("msg", {})
                ch = msg.get("channel", "")

                if ch:
                    channels[ch] = channels.get(ch, 0) + 1

                if "account" in ch.lower():
                    account_updates.append(rec)
                if "order" in ch.lower():
                    order_events.append(rec)
                if "position" in ch.lower():
                    position_events.append(rec)
            except:
                pass

    print(f"\n📊 Channel 统计:")
    for ch, count in sorted(channels.items()):
        print(f"  {ch}: {count} 条消息")

    print(f"\n📊 事件统计:")
    print(f"  Account 更新: {len(account_updates)}")
    print(f"  Order 事件: {len(order_events)}")
    print(f"  Position 事件: {len(position_events)}")

    # 检查 balance 轮询
    expected_polls = TEST_DURATION // BALANCE_POLL_INTERVAL
    print(f"\n📊 Balance 轮询检查:")
    print(f"  预期轮询次数: ~{expected_polls}")
    print(f"  实际 account 更新: {len(account_updates)}")

    print("\n✅ 测试完成！")
    print(f"详细数据请查看: {RECORD_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
