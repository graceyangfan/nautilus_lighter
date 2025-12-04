#!/usr/bin/env python3
"""测试订单成交、止盈止损
- post_only=True 挂单在盘口 level 2-3
- 止盈单靠近当前价（期待成交）
- 止损单远离当前价（避免成交）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 直接使用 exec_client_command_ws.py 的逻辑，但修改订单策略
from exec_client_command_ws import main as original_main

# 设置环境变量
os.environ["LIGHTER_ACCOUNT_INDEX"] = "XXXXX"
os.environ["LIGHTER_API_KEY_INDEX"] = "2"
os.environ["LIGHTER_EXEC_RECORD"] = "/tmp/fill_tp_sl_test.jsonl"

print("=" * 80)
print("测试：订单成交 + 止盈止损")
print("=" * 80)
print("策略：")
print("1. post_only=True 挂单在盘口 level 2-3（期待成交）")
print("2. 成交后挂止盈单（靠近当前价）")
print("3. 成交后挂止损单（远离当前价）")
print("4. 运行 180 秒观察")
print("=" * 80)
print()

# 运行测试（180秒）
sys.argv = ["test_fill_and_tp_sl.py", "--runtime", "180", "--post-only"]
asyncio.run(original_main())

print("\n" + "=" * 80)
print("测试完成！")
print("录制文件: /tmp/fill_tp_sl_test.jsonl")
print("=" * 80)
