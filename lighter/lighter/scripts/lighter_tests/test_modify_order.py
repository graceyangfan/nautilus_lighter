#!/usr/bin/env python3
import asyncio, os
from decimal import Decimal
from nautilus_trader.adapters.lighter.common.signer import LighterCredentials
from nautilus_trader.adapters.lighter.config import LighterExecClientConfig
from nautilus_trader.adapters.lighter.http.public import LighterPublicHttpClient
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.execution import LighterExecutionClient
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock, MessageBus
from nautilus_trader.model.identifiers import TraderId, StrategyId, ClientOrderId
from nautilus_trader.execution.messages import ModifyOrder
from nautilus_trader.model.objects import Price, Quantity
from nautilus_trader.core.uuid import UUID4

async def main():
    loop, clock = asyncio.get_event_loop(), LiveClock()
    trader, strat = TraderId("TRADER-MOD"), StrategyId("STRAT-MOD")
    msgbus, cache = MessageBus(trader, clock), Cache()

    http_pub = LighterPublicHttpClient(base_url="https://mainnet.zklighter.elliot.ai")
    provider = LighterInstrumentProvider(client=http_pub, concurrency=1)
    await provider.load_all_async(filters={"bases": ["BNB"]})
    inst_id, inst = next(iter(provider.get_all().items()))
    
    creds = LighterCredentials(
        pubkey=os.getenv("LIGHTER_PUBLIC_KEY"),
        account_index=int(os.getenv("LIGHTER_ACCOUNT_INDEX", "XXXXX")),
        api_key_index=int(os.getenv("LIGHTER_API_KEY_INDEX", "2")),
        private_key=os.getenv("LIGHTER_PRIVATE_KEY"),
    )
    
    exec_client = LighterExecutionClient(
        loop=loop, msgbus=msgbus, cache=cache, clock=clock,
        instrument_provider=provider,
        base_url_http="https://mainnet.zklighter.elliot.ai",
        base_url_ws="wss://mainnet.zklighter.elliot.ai/stream",
        credentials=creds,
        config=LighterExecClientConfig(
            base_url_http="https://mainnet.zklighter.elliot.ai",
            base_url_ws="wss://mainnet.zklighter.elliot.ai/stream",
            credentials=creds,
            chain_id=304,
            subscribe_account_stats=False,
            use_python_ws_private=True,
        ),
    )

    print("Connecting...")
    await exec_client._connect()
    await asyncio.sleep(3)
    
    # Find open order from cache
    client_order_id = ClientOrderId("O-20250126-022914-001-001-1")  # From recorded data
    
    print(f"\nModifying order {client_order_id} to price=870, qty=0.02")
    
    modify_cmd = ModifyOrder(
        trader_id=trader,
        strategy_id=strat,
        instrument_id=inst_id,
        client_order_id=client_order_id,
        venue_order_id=None,
        quantity=Quantity.from_str("0.02"),
        price=Price.from_str("870"),
        trigger_price=None,
        command_id=UUID4(),
        ts_init=clock.timestamp_ns(),
    )
    
    exec_client.modify_order(modify_cmd)
    
    print("Waiting for updates...")
    await asyncio.sleep(10)
    
    await exec_client._disconnect()
    print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
