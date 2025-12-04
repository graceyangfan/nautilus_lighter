#!/usr/bin/env python3
"""
Standalone Lighter batch place + cancel + account states test (WS only).

- Avoids importing the full nautilus_trader package (no compiled deps needed).
- Dynamically loads the adapter's signer code by file path.
- Uses websockets to connect to the WS endpoint and send jsonapi messages.

Env (defaults target testnet):
  LIGHTER_HTTP=https://testnet.zklighter.elliot.ai
  LIGHTER_WS=wss://testnet.zklighter.elliot.ai/stream
  LIGHTER_CREDENTIALS_JSON=secrets/lighter_testnet_account.json
  LIGHTER_MARKET_INDEX=0             # ETH often more active
  LIGHTER_BASE_AMOUNT=50             # scaled size (ETH 0.0050 -> 50)
  LIGHTER_PRICE_MULT=0.80            # far price to avoid fill
  LIGHTER_RUN_SECS=60

Notes:
  - Will auto-download lighter-go signer shared library to ~/.nautilus_trader if missing.
  - If the go signer cannot load, script exits.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import random
import time
from pathlib import Path
from typing import Any

import ssl

try:
    import websockets  # type: ignore
except Exception as e:  # pragma: no cover - handled by caller
    raise SystemExit("Please install websockets: pip install websockets") from e


ROOT = Path(__file__).resolve().parent.parent
SIGNER_PATH = ROOT / "nautilus_trader" / "adapters" / "lighter" / "common" / "signer.py"


def load_signer_module():
    spec = importlib.util.spec_from_file_location("lighter_signer", str(SIGNER_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load signer module")
    mod = importlib.util.module_from_spec(spec)
    import sys as _sys
    _sys.modules[spec.name] = mod  # ensure visible for dataclasses typing lookups
    spec.loader.exec_module(mod)  # type: ignore[arg-type]
    return mod


def _to_int(x: str | int | None) -> int:
    if x is None:
        return 0
    s = str(x)
    return int(s, 16) if s.startswith("0x") else int(s)


async def main() -> None:
    http = os.getenv("LIGHTER_HTTP", "https://testnet.zklighter.elliot.ai").rstrip("/")
    ws_url = os.getenv("LIGHTER_WS", http.replace("https", "wss") + "/stream")
    creds_path = os.getenv("LIGHTER_CREDENTIALS_JSON", "secrets/lighter_testnet_account.json")
    market_index = int(os.getenv("LIGHTER_MARKET_INDEX", "0"))
    base_amount = int(os.getenv("LIGHTER_BASE_AMOUNT", "50"))
    price_mult = float(os.getenv("LIGHTER_PRICE_MULT", "0.80"))
    run_secs = int(os.getenv("LIGHTER_RUN_SECS", "60"))

    with open(creds_path, "r", encoding="utf-8") as f:
        cj = json.load(f)
    # Dynamic import of signer module
    signer_mod = load_signer_module()
    LighterCredentials = signer_mod.LighterCredentials
    LighterSigner = signer_mod.LighterSigner

    creds = LighterCredentials(
        pubkey=str(cj.get("pubkey")),
        account_index=_to_int(cj.get("account_index")),
        api_key_index=_to_int(cj.get("api_key_index")),
        nonce=_to_int(cj.get("nonce")),
        private_key=(cj.get("private_key") or os.getenv("LIGHTER_PRIVATE_KEY")),
    )
    signer = LighterSigner(credentials=creds, base_url=http)
    if not signer.available():
        raise SystemExit("lighter-go signer unavailable. Place the platform-specific binary under adapters/lighter/signers/ and retry.")

    # Helper: fetch best bid/ask via simple urllib
    import urllib.request

    def get_best_prices(mid: int) -> tuple[int | None, int | None]:
        try:
            url = f"{http}/api/v1/orderBookOrders?market_id={mid}&limit=5"
            with urllib.request.urlopen(url, timeout=10) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            def parse_first(arr: list[dict] | None) -> int | None:
                if not isinstance(arr, list) or not arr:
                    return None
                x = arr[0].get("price")
                if x is None:
                    return None
                s = str(x)
                return int(s.replace(".", "")) if "." in s else int(s)
            return parse_first(doc.get("bids")), parse_first(doc.get("asks"))
        except Exception:
            return None, None

    best_bid, best_ask = get_best_prices(market_index)
    if best_bid is None and best_ask is None:
        print("[WARN] cannot fetch order book; using default scaled price 200000")
        best_bid = 200000
        best_ask = 201000

    # Fetch and print account snapshot via HTTP (requires auth token)
    try:
        tok = signer.create_auth_token()
        import urllib.parse
        url = f"{http}/api/v1/account?" + urllib.parse.urlencode({"by": "index", "value": str(creds.account_index), "auth": tok})
        with urllib.request.urlopen(url, timeout=10) as resp:
            acc = json.loads(resp.read().decode("utf-8"))
        print("[HTTP] account snapshot keys:", list((acc.get("account") or acc).keys()))
    except Exception as e:
        print("[HTTP] account snapshot fetch failed:", e)

    # Choose two client_order_index values
    # Lighter requires client_order_index <= 2^48-1
    now = int(time.time() * 1000)  # millis
    mask48 = (1 << 48) - 1
    base = now & mask48
    coi1 = (base ^ random.randrange(1, 1 << 20)) & mask48
    coi2 = (base ^ random.randrange(1 << 20, 1 << 21)) & mask48

    far_price = int((best_bid or 200000) * price_mult)
    near_price = int((best_ask or 201000) * (1 + 0.10))  # 10% above ask to avoid fill (buy side)

    # Helper: fetch sequential nonces via HTTP to avoid signer internal lookup
    def fetch_next_nonce() -> int:
        try:
            import urllib.parse, urllib.request
            url = f"{http}/api/v1/nextNonce?" + urllib.parse.urlencode({
                "account_index": creds.account_index,
                "api_key_index": creds.api_key_index,
            })
            with urllib.request.urlopen(url, timeout=10) as resp:
                doc = json.loads(resp.read().decode("utf-8"))
            raw = doc.get("next_nonce") or doc.get("nonce")
            if isinstance(raw, str) and raw.startswith("0x"):
                return int(raw, 16)
            return int(raw)
        except Exception as e:  # pragma: no cover - depends on network
            raise SystemExit(f"Fetch nextNonce failed: {e}")

    nonce0 = fetch_next_nonce()
    nonce1 = nonce0
    nonce2 = nonce0 + 1

    # Prepare two LIMIT GTT buy orders (reduce_only=false)
    tx1, err1 = await signer.sign_create_order_tx(
        market_index=market_index,
        client_order_index=coi1,
        base_amount=base_amount,
        price=far_price,
        is_ask=False,
        order_type=0,      # LIMIT
        time_in_force=1,   # GTT
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=nonce1,
    )
    if err1 or not tx1:
        raise SystemExit(f"sign_create_order #1 error: {err1}")

    tx2, err2 = await signer.sign_create_order_tx(
        market_index=market_index,
        client_order_index=coi2,
        base_amount=base_amount,
        price=near_price,
        is_ask=False,
        order_type=0,
        time_in_force=1,
        reduce_only=False,
        trigger_price=0,
        order_expiry=-1,
        nonce=nonce2,
    )
    if err2 or not tx2:
        raise SystemExit(f"sign_create_order #2 error: {err2}")

    # Connect WS
    ssl_ctx = ssl.create_default_context()
    async with websockets.connect(ws_url, ssl=ssl_ctx) as ws:
        # Wait first server message (usually {"type":"connected"})
        try:
            msg0 = await asyncio.wait_for(ws.recv(), timeout=5)
            try:
                obj0 = json.loads(msg0)
                print("[WS0]", obj0)
            except Exception:
                pass
        except Exception:
            pass

        # Subscribe to account channels with auth
        token = signer.create_auth_token()
        acct = creds.account_index
        subs = [
            {"type": "subscribe", "channel": f"account_all/{acct}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_orders/{acct}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_trades/{acct}", "auth": token},
            {"type": "subscribe", "channel": f"account_all_positions/{acct}", "auth": token},
        ]
        for sub in subs:
            await ws.send(json.dumps(sub))

        # Batch create (2 orders)
        create_types_str = json.dumps([signer.get_tx_type_create_order(), signer.get_tx_type_create_order()])
        create_infos_str = json.dumps([tx1, tx2])
        await ws.send(
            json.dumps(
                {
                    "type": "jsonapi/sendtxbatch",
                    "data": {
                        "id": f"batch-{time.time_ns()}",
                        "tx_types": create_types_str,
                        "tx_infos": create_infos_str,
                        "auth": token,
                    },
                }
            )
        )
        print("[SEND] jsonapi/sendtxbatch (2x create)")

        # Also try REST /api/v1/sendTxBatch as fallback/verification
        try:
            tok2 = token
            import urllib.request
            from urllib.parse import urlencode
            req = urllib.request.Request(
                f"{http}/api/v1/sendTxBatch",
                data=urlencode({
                    "tx_types": create_types_str,
                    "tx_infos": create_infos_str,
                    "auth": tok2,
                }).encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            print("[HTTP] sendTxBatch response keys:", list(body.keys()))
        except Exception as e:
            print("[HTTP] sendTxBatch failed:", e)

        # Observe for a while; capture mapping client_order_index -> order_index
        coi_to_oi: dict[int, int] = {}
        deadline = time.time() + max(30, run_secs // 2)
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            t = obj.get("type")
            ch = obj.get("channel")
            if t == "jsonapi/sendtx_result":
                print("[WS] sendtx_result", obj)
            if t == "jsonapi/sendtxbatch_result":
                print("[WS] sendtxbatch_result", obj)
            if isinstance(ch, str) and ch.startswith("account_all_orders:"):
                data = obj.get("data") or {}
                for od in data.get("orders", []) or []:
                    try:
                        coi = int(str(od.get("client_order_index")))
                        oi = int(str(od.get("order_index"))) if od.get("order_index") is not None else None
                        if oi is not None:
                            coi_to_oi[coi] = oi
                            print("[OBS] coi -> oi:", coi, "->", oi, "status=", od.get("status"))
                    except Exception:
                        pass
            if isinstance(ch, str) and ch.startswith("account_stats:"):
                print("[WS] account_stats keys:", list((obj.get("data") or {}).keys()))

        # Prepare batch cancel
        cancel_tx_types: list[int] = []
        cancel_tx_infos: list[str] = []
        # For cancels, fetch fresh base nonce
        base_cancel_nonce = fetch_next_nonce()
        for idx, coi in enumerate((coi1, coi2)):
            oi = coi_to_oi.get(coi)
            if oi is None:
                continue
            tx_c, errc = await signer.sign_cancel_order_tx(
                market_index=market_index,
                order_index=int(oi),
                nonce=base_cancel_nonce + idx,
            )
            if errc or not tx_c:
                print("[WARN] sign_cancel_order error for", oi, errc)
                continue
            cancel_tx_types.append(signer.get_tx_type_cancel_order())
            cancel_tx_infos.append(tx_c)

        if cancel_tx_types:
            await ws.send(
                json.dumps(
                    {
                        "type": "jsonapi/sendtxbatch",
                        "data": {
                            "id": f"cancel-batch-{time.time_ns()}",
                            "tx_types": json.dumps(cancel_tx_types),
                            "tx_infos": json.dumps(cancel_tx_infos),
                            "auth": token,
                        },
                    }
                )
            )
            print(f"[SEND] jsonapi/sendtxbatch ({len(cancel_tx_types)}x cancel)")
        else:
            # Fallback: cancel-all
            from nautilus_trader.adapters.lighter.common.enums import LighterCancelAllTif
            tx_all, erra = await signer.sign_cancel_all_orders_tx(time_in_force=LighterCancelAllTif.IMMEDIATE, time=0, nonce=None)
            if tx_all and not erra:
                await ws.send(json.dumps({"type": "jsonapi/sendtx", "data": {"id": f"local-{time.time_ns()}", "tx_type": signer.get_tx_type_cancel_all(), "tx_info": json.loads(tx_all)}}))
                print("[SEND] jsonapi/sendtx (cancel-all)")
                # Try REST cancel-all as well
                try:
                    tok3 = token
                    import urllib.request
                    from urllib.parse import urlencode
                    req2 = urllib.request.Request(
                        f"{http}/api/v1/sendTx",
                        data=urlencode({
                            "tx_type": str(signer.get_tx_type_cancel_all()),
                            "tx_info": tx_all,
                            "auth": tok3,
                        }).encode("utf-8"),
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req2, timeout=15) as resp:
                        body2 = json.loads(resp.read().decode("utf-8"))
                    print("[HTTP] sendTx cancel-all keys:", list(body2.keys()))
                except Exception as e:
                    print("[HTTP] sendTx cancel-all failed:", e)

        # Final observation window
        end_deadline = time.time() + max(30, run_secs // 2)
        while time.time() < end_deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            t = obj.get("type")
            ch = obj.get("channel")
            if t == "jsonapi/sendtx_result":
                print("[WS] sendtx_result", obj)
            if isinstance(ch, str) and (ch.startswith("account_all_orders:") or ch.startswith("account_stats:")):
                print("[WS]", t, ch)
            else:
                # Print any jsonapi
                if isinstance(t, str) and t.startswith("jsonapi/"):
                    print("[WS]", t, obj)

        print("[DONE] market_index=", market_index, "coi_to_oi=", coi_to_oi)


if __name__ == "__main__":
    asyncio.run(main())
