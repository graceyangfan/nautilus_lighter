#!/usr/bin/env python3
"""
Lighter-Go signer adapter (JSON stdin/stdout).

Purpose
- Bridge the adapter's `LIGHTER_SIGNER_CMD` JSON contract to a lighter-go binary.
- You can control the exact CLI invocation via environment templates, so this
  adapter works with your specific lighter-go build without code changes.

Usage
- Set `LIGHTER_SIGNER_CMD="python scripts/lighter_go_signer_adapter.py"` (or
  the absolute path), and provide the templates below as env vars if defaults
  don't match your binary.

JSON contract (stdin → stdout)
- See docs: the adapter sends operations like `create_auth_token`, `sign_create_order`,
  etc. This script:
  - Builds a CLI command from an environment template.
  - Runs it via subprocess and returns JSON.
  - If the binary prints a raw tx_info string, the adapter wraps it as `{"tx_info": "..."}`.

Environment variables (templates)
- `LIGHTER_GO_BIN` (default: `lighter-go`)
- `LIGHTER_GO_TEMPLATE_CREATE_AUTH`
- `LIGHTER_GO_TEMPLATE_SIGN_CREATE_ORDER`
- `LIGHTER_GO_TEMPLATE_SIGN_CANCEL_ORDER`
- `LIGHTER_GO_TEMPLATE_SIGN_CANCEL_ALL`
- `LIGHTER_GO_TEMPLATE_SIGN_CREATE_TP_LIMIT`

Templates use Python `%` formatting and receive a context dict with keys:
- base_url, private_key, account_index, api_key_index, deadline_seconds, and
  everything under `args` for the operation (e.g., market_index, client_order_index, ...).

Example template (adjust to your binary):
  LIGHTER_GO_TEMPLATE_SIGN_CREATE_ORDER="%(bin)s sign-create-order \
    --base-url %(base_url)s --account-index %(account_index)d --api-key-index %(api_key_index)d \
    --private-key %(private_key)s --market-index %(market_index)d --client-order-index %(client_order_index)d \
    --base-amount %(base_amount)d --price %(price)d --is-ask %(is_ask)d --order-type %(order_type)d \
    --time-in-force %(time_in_force)d --reduce-only %(reduce_only)d --trigger-price %(trigger_price)d \
    --order-expiry %(order_expiry)d --nonce %(nonce)s"

Note: If your binary accepts JSON stdin instead, point the template to something like:
  "%(bin)s sign --json"
and this adapter will pass JSON via stdin and forward stdout.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import subprocess
from typing import Any


DEFAULTS = {
    "TX_TYPE_CREATE_ORDER": 14,
    "TX_TYPE_CANCEL_ORDER": 15,
    "TX_TYPE_CANCEL_ALL_ORDERS": 16,
    "TX_TYPE_TAKE_PROFIT_LIMIT": 5,
}


def _ctx(payload: dict[str, Any]) -> dict[str, Any]:
    base = {
        "bin": os.getenv("LIGHTER_GO_BIN", "lighter-go"),
        "base_url": payload.get("base_url") or "",
        "private_key": payload.get("private_key") or "",
        "account_index": int(payload.get("account_index") or 0),
        "api_key_index": int(payload.get("api_key_index") or 0),
        "deadline_seconds": int(payload.get("deadline_seconds") or 600),
    }
    args = payload.get("args") or {}
    for k, v in dict(args).items():
        base[k] = v
    return base


def _run_cmd(template_env: str, payload: dict[str, Any], stdin_json: dict[str, Any] | None = None) -> tuple[str, str, int]:
    tpl = os.getenv(template_env)
    if not tpl:
        return "", f"template {template_env} not set", 127
    ctx = _ctx(payload)
    try:
        cmd = tpl % ctx
    except Exception as e:
        return "", f"template format error: {e}", 127

    # If template includes a marker to pass stdin JSON, we detect by placeholder
    # Alternatively, always allow passing stdin_json if provided
    proc = subprocess.run(
        cmd if isinstance(cmd, str) else shlex.join(cmd),
        input=(json.dumps(stdin_json) if stdin_json is not None else None),
        capture_output=True,
        text=True,
        shell=True,
        timeout=60,
    )
    return proc.stdout, proc.stderr, proc.returncode


def _emit(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.write("\n")
    sys.stdout.flush()


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        _emit({"error": f"invalid json input: {e}"})
        return

    op = payload.get("op")
    if op == "ping":
        _emit({"ok": True})
        return
    if op == "get_constants":
        _emit(DEFAULTS)
        return

    if op == "create_auth_token":
        out, err, code = _run_cmd("LIGHTER_GO_TEMPLATE_CREATE_AUTH", payload, stdin_json=payload)
        if code != 0:
            _emit({"error": err or out or f"exit {code}"})
            return
        # Try parse JSON, else treat whole stdout as token
        try:
            doc = json.loads(out)
            token = doc.get("auth_token") or doc.get("token") or doc.get("auth") or out.strip()
        except Exception:
            token = out.strip()
        _emit({"auth_token": token})
        return

    if op == "sign_create_order":
        out, err, code = _run_cmd("LIGHTER_GO_TEMPLATE_SIGN_CREATE_ORDER", payload, stdin_json=payload)
        if code != 0:
            _emit({"error": err or out or f"exit {code}"})
            return
        try:
            doc = json.loads(out)
            tx_info = doc.get("tx_info") or out.strip()
        except Exception:
            tx_info = out.strip()
        _emit({"tx_info": tx_info})
        return

    if op == "sign_cancel_order":
        out, err, code = _run_cmd("LIGHTER_GO_TEMPLATE_SIGN_CANCEL_ORDER", payload, stdin_json=payload)
        if code != 0:
            _emit({"error": err or out or f"exit {code}"})
            return
        try:
            doc = json.loads(out)
            tx_info = doc.get("tx_info") or out.strip()
        except Exception:
            tx_info = out.strip()
        _emit({"tx_info": tx_info})
        return

    if op == "sign_cancel_all_orders":
        out, err, code = _run_cmd("LIGHTER_GO_TEMPLATE_SIGN_CANCEL_ALL", payload, stdin_json=payload)
        if code != 0:
            _emit({"error": err or out or f"exit {code}"})
            return
        try:
            doc = json.loads(out)
            tx_info = doc.get("tx_info") or out.strip()
        except Exception:
            tx_info = out.strip()
        _emit({"tx_info": tx_info})
        return

    if op == "sign_create_tp_limit":
        out, err, code = _run_cmd("LIGHTER_GO_TEMPLATE_SIGN_CREATE_TP_LIMIT", payload, stdin_json=payload)
        if code != 0:
            _emit({"error": err or out or f"exit {code}"})
            return
        try:
            doc = json.loads(out)
            tx_info = doc.get("tx_info") or out.strip()
        except Exception:
            tx_info = out.strip()
        _emit({"tx_info": tx_info, "tx_type": DEFAULTS["TX_TYPE_TAKE_PROFIT_LIMIT"]})
        return

    _emit({"error": f"unsupported op: {op}"})


if __name__ == "__main__":
    main()

