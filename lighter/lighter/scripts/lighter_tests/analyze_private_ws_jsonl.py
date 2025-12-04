#!/usr/bin/env python3
from __future__ import annotations

"""
Analyze a recorded private WS JSONL file and report duplication metrics for
orders/trades/positions.

Input is a JSONL produced by scripts/lighter_tests/record_private_ws.py.

Usage:
  python scripts/lighter_tests/analyze_private_ws_jsonl.py \
    --jsonl /tmp/lighter_priv.jsonl
"""

import argparse
import json
from collections import defaultdict
from typing import Any


def _flatten(obj: Any, key: str) -> list[dict]:
    if not isinstance(obj, dict):
        return []
    payload = obj.get('msg') if isinstance(obj.get('msg'), dict) else obj
    src = payload
    if isinstance(payload.get('data'), dict):
        src = payload['data']
    val = src.get(key)
    out: list[dict] = []
    if isinstance(val, list):
        out = [x for x in val if isinstance(x, dict)]
    elif isinstance(val, dict):
        for v in val.values():
            if isinstance(v, list):
                out.extend([x for x in v if isinstance(x, dict)])
            elif isinstance(v, dict):
                out.append(v)
    return out


def _normalize(d: dict) -> dict:
    # Make a shallow-normalized dict for equality compare; keep only stable fields
    return {k: d.get(k) for k in sorted(d.keys())}


def analyze(path: str) -> None:
    trades_by_id: dict[int, list[dict]] = defaultdict(list)
    orders_by_id: dict[int, list[dict]] = defaultdict(list)
    positions_by_mkt: dict[int, list[dict]] = defaultdict(list)
    frames = 0
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            frames += 1
            for t in _flatten(rec, 'trades'):
                try:
                    tid = int(t.get('trade_id'))
                    trades_by_id[tid].append(_normalize(t))
                except Exception:
                    continue
            for o in _flatten(rec, 'orders'):
                try:
                    oid = int(o.get('order_index') or o.get('order_id'))
                    orders_by_id[oid].append(_normalize(o))
                except Exception:
                    continue
            for p in _flatten(rec, 'positions'):
                try:
                    mid = int(p.get('market_index') or p.get('market_id'))
                    positions_by_mkt[mid].append(_normalize(p))
                except Exception:
                    continue

    def summarize(bucket: dict[int, list[dict]], name: str, id_name: str = 'id') -> None:
        total = sum(len(v) for v in bucket.values())
        unique = len(bucket)
        dups = sum(1 for v in bucket.values() if len(v) > 1)
        same = 0
        changed = 0
        for vs in bucket.values():
            if len(vs) <= 1:
                continue
            base = vs[0]
            all_same = all(v == base for v in vs[1:])
            if all_same:
                same += 1
            else:
                changed += 1
        print(f"[{name}] frames={frames} total_items={total} unique_{id_name}s={unique} dup_{id_name}s={dups} dup_same={same} dup_changed={changed}")

    summarize(trades_by_id, 'trades', 'trade_id')
    summarize(orders_by_id, 'orders', 'order_index')
    summarize(positions_by_mkt, 'positions', 'market')


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--jsonl', required=True)
    args = ap.parse_args()
    analyze(args.jsonl)


if __name__ == '__main__':
    main()

