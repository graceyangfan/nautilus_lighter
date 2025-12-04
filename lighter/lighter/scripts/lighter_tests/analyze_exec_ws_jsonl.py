#!/usr/bin/env python3
from __future__ import annotations

"""
Analyze a JSONL file recorded from LighterExecutionClient private WS traffic.

The script focuses on:
  - `jsonapi/sendtx*` frames (single and batch) to inspect status / code /
    order_index / client_order_index.
  - `account_all_orders` frames to show per-order status, price and remaining
    base amount over time.

Typical input is a JSONL produced via the `LIGHTER_EXEC_RECORD` environment
variable on `LighterExecutionClient` (for example when running
`exec_modify_cases.py` or `exec_batch_place_cancel.py`).

Usage:
  python scripts/lighter_tests/analyze_exec_ws_jsonl.py \\
    --jsonl /tmp/nautilus_exec_ws.jsonl
"""

import argparse
import json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default="/tmp/nautilus_exec_ws.jsonl")
    args = ap.parse_args()

    path = args.jsonl
    print(f"[ANALYZE] file={path}")

    sendtx_count = 0
    order_frames = 0

    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            msg = rec.get("msg", {})
            if not isinstance(msg, dict):
                continue

            t = str(msg.get("type") or "")
            ch = str(msg.get("channel") or "")

            # jsonapi/sendtx[_result] / sendtxbatch[_result] frames
            if t.startswith("jsonapi/sendtx"):
                sendtx_count += 1
                d = msg.get("data", {}) or {}
                status = d.get("status")
                code = d.get("code")
                oi = d.get("order_index") or d.get("orderId")
                coi = d.get("client_order_index")
                print(f"[{i}] {t}: status={status} code={code} oi={oi} coi={coi}")

            # account_all_orders updates
            if "account_all_orders" in ch:
                order_frames += 1
                orders = msg.get("orders", {})
                print(f"\n[{i}] account_all_orders frame #{order_frames} type={t}")
                if isinstance(orders, dict):
                    for mid, lst in orders.items():
                        if isinstance(lst, list):
                            for o in lst:
                                try:
                                    oi = o.get("order_index")
                                    coi = o.get("client_order_index")
                                    status = o.get("status")
                                    price = o.get("price")
                                    rem = o.get("remaining_base_amount")
                                    print(
                                        f"    mid={mid} oi={oi} coi={coi} "
                                        f"status={status} price={price} remaining={rem}"
                                    )
                                except Exception:
                                    continue

    print(f"\n[ANALYZE] sendtx frames: {sendtx_count}")
    print(f"[ANALYZE] account_all_orders frames: {order_frames}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

