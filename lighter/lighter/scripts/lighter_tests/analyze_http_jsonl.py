#!/usr/bin/env python3
from __future__ import annotations

"""
Analyze recorded Lighter HTTP JSONL (LIGHTER_HTTP_RECORD).

This script groups responses by path and prints simple statistics:
  - number of responses per path
  - distinct HTTP status codes observed
  - for JSON bodies, the top-level keys seen

Usage:

  python adapters/lighter/scripts/lighter_tests/analyze_http_jsonl.py \\
    --jsonl /tmp/lighter_http_mainnet.jsonl
"""

import argparse
import json
from collections import defaultdict
from typing import Any


def analyze(path: str) -> None:
    stats: dict[str, dict[str, Any]] = {}

    def ensure_entry(p: str) -> dict[str, Any]:
        if p not in stats:
            stats[p] = {
                "count": 0,
                "statuses": set(),
                "keys": set(),
            }
        return stats[p]

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            p = str(rec.get("path") or "")
            entry = ensure_entry(p)
            entry["count"] += 1
            entry["statuses"].add(int(rec.get("status") or 0))
            body = rec.get("body")
            if isinstance(body, dict):
                for k in body.keys():
                    entry["keys"].add(str(k))

    print(f"[ANALYZE-HTTP] file={path}")
    for p, e in sorted(stats.items()):
        statuses = ",".join(str(s) for s in sorted(e["statuses"]))
        keys = ",".join(sorted(e["keys"]))
        print(f"  path={p} count={e['count']} statuses={statuses} keys={keys}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    args = ap.parse_args()
    analyze(args.jsonl)


if __name__ == "__main__":
    main()

