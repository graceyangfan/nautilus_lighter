#!/usr/bin/env python3
from __future__ import annotations

"""
Extract the first account_stats frame from a JSONL recording into the repo fixture.

Usage:
  python scripts/lighter_tests/extract_account_stats_fixture.py \
    --jsonl /tmp/lighter_priv.jsonl \
    --out tests/integration_tests/adapters/lighter/fixtures/account_stats_update.json
"""

import argparse
import json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--jsonl', required=True)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    src = Path(args.jsonl)
    dst = Path(args.out)
    if not src.exists():
        print('Source JSONL not found:', src)
        return 1

    msg = None
    with src.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            obj = rec.get('msg') if isinstance(rec, dict) else None
            if not isinstance(obj, dict):
                continue
            ch = str(obj.get('channel') or '')
            if ch.startswith('account_stats:'):
                msg = obj
                break

    if msg is None:
        print('No account_stats frame found in', src)
        return 2

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(msg, ensure_ascii=False, indent=2))
    print('Wrote fixture to', dst)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

