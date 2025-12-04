#!/usr/bin/env python3
"""简单测试 account_stats 是否可用

直接使用 exec_client_command_ws.py 的逻辑
"""
import asyncio
import json
import os
import sys

# 添加路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exec_client_command_ws import main as exec_main

# 设置环境变量
os.environ["LIGHTER_ACCOUNT_INDEX"] = "XXXXX"
os.environ["LIGHTER_API_KEY_INDEX"] = "2"
os.environ["LIGHTER_EXEC_RECORD"] = "/tmp/account_stats_mainnet.jsonl"

print("=== Testing account_stats channel ===")
print("Will place and cancel an order to trigger balance changes")
print("Recording to: /tmp/account_stats_mainnet.jsonl")
print()

# 运行测试
asyncio.run(exec_main())

# 分析结果
print("\n=== Analyzing results ===")
account_stats_found = False
channels = set()

with open("/tmp/account_stats_mainnet.jsonl") as f:
    for line in f:
        try:
            rec = json.loads(line)
            msg = rec.get("msg", {})
            ch = msg.get("channel", "")

            if ch:
                channels.add(ch)

            if "account_stats" in ch:
                account_stats_found = True
                print(f"✅ Found account_stats message!")
                print(json.dumps(msg, indent=2)[:800])
                break
        except:
            pass

print(f"\nChannels seen: {sorted(channels)}")

if account_stats_found:
    print("\n✅ SUCCESS: account_stats channel is available!")
else:
    print("\n❌ account_stats channel not found")
    print("Possible reasons:")
    print("1. Channel not available")
    print("2. No balance change during test")
    print("3. Need to check subscription code")
