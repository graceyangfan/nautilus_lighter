#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys
import types
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# -----------------------------------------------------------------------------------
# Minimal import shim to load only lighter signer code without importing project root
# -----------------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]

def _ensure_pkg(name: str, pkg_path: Path) -> None:
    if name in sys.modules:
        return
    mod = types.ModuleType(name)
    mod.__path__ = [str(pkg_path)]  # type: ignore[attr-defined]
    sys.modules[name] = mod

_ensure_pkg('nautilus_trader', REPO_ROOT / 'nautilus_trader')
_ensure_pkg('nautilus_trader.adapters', REPO_ROOT / 'nautilus_trader' / 'adapters')
_ensure_pkg('nautilus_trader.adapters.lighter', REPO_ROOT / 'nautilus_trader' / 'adapters' / 'lighter')
_ensure_pkg('nautilus_trader.adapters.lighter.common', REPO_ROOT / 'nautilus_trader' / 'adapters' / 'lighter' / 'common')

# Stub out model enums to avoid importing compiled core package
if 'nautilus_trader.model' not in sys.modules:
    sys.modules['nautilus_trader.model'] = types.ModuleType('nautilus_trader.model')
model_enums = types.ModuleType('nautilus_trader.model.enums')
class _OrderType: pass
class _TimeInForce: pass
setattr(model_enums, 'OrderType', _OrderType)
setattr(model_enums, 'TimeInForce', _TimeInForce)
sys.modules['nautilus_trader.model.enums'] = model_enums

from nautilus_trader.adapters.lighter.common.signer import LighterCredentials, LighterSigner  # type: ignore


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v is not None else default


def _http_get_json(base_url: str, path: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{base_url.rstrip('/')}{path}{('?' + q) if q else ''}"
    req = urllib.request.Request(url, method='GET', headers={"Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310 (tooling script)
        raw = resp.read()
    try:
        return json.loads(raw.decode('utf-8')) if raw else {}
    except Exception:
        return {"raw": raw.decode('utf-8', errors='ignore')}


def _http_post_form_json(base_url: str, path: str, form: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode('utf-8')
    url = f"{base_url.rstrip('/')}{path}"
    req = urllib.request.Request(url, method='POST', data=data, headers={
        'Accept': 'application/json',
        'Content-Type': 'application/x-www-form-urlencoded',
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            raw = resp.read()
            status = resp.getcode() or 0
    except Exception as exc:
        raise RuntimeError(f"HTTP POST failed: {exc}")
    try:
        payload = json.loads(raw.decode('utf-8')) if raw else {}
    except Exception:
        payload = {"raw": raw.decode('utf-8', errors='ignore')}
    if isinstance(payload, dict) and int(payload.get('code', 200)) != 200:
        raise RuntimeError(f"Lighter API error code={payload.get('code')} msg={payload.get('message')}")
    return payload


async def main() -> None:
    base_url = _env("LIGHTER_BASE_URL", "https://testnet.zklighter.elliot.ai")
    account_index_s = _env("LIGHTER_ACCOUNT_INDEX")
    api_key_index_s = _env("LIGHTER_API_KEY_INDEX")
    pubkey = _env("LIGHTER_PUBKEY")
    priv = _env("LIGHTER_PRIVATE_KEY")
    # Fallback to secrets file if env not provided
    if not (pubkey and priv and account_index_s and api_key_index_s):
        secrets_path = REPO_ROOT / 'secrets' / 'lighter_testnet_account.json'
        if secrets_path.exists():
            try:
                data = json.loads(secrets_path.read_text(encoding='utf-8'))
                pubkey = pubkey or data.get('pubkey')
                priv = priv or data.get('private_key')
                account_index_s = account_index_s or str(data.get('account_index'))
                api_key_index_s = api_key_index_s or str(data.get('api_key_index'))
                print(f"Loaded creds from {secrets_path.name}")
            except Exception as exc:
                print(f"Failed to load {secrets_path}: {exc}")

    print(f"Base URL: {base_url}")

    # 1) Public endpoint sanity (raw HTTP)
    try:
        payload = _http_get_json(base_url, "/api/v1/orderBooks")
        books = payload.get("order_books") or []
        print(f"Public get_order_books: {len(books)} markets")
    except Exception as exc:
        print(f"Public get_order_books error: {exc}")

    # 2) Signer availability and optional auth token
    token: str | None = None
    signer: LighterSigner | None = None
    if pubkey and account_index_s and api_key_index_s and priv:
        try:
            creds = LighterCredentials(
                pubkey=pubkey,
                account_index=int(account_index_s, 0) if account_index_s.startswith("0x") else int(account_index_s),
                api_key_index=int(api_key_index_s, 0) if api_key_index_s.startswith("0x") else int(api_key_index_s),
                private_key=priv,
            )
            signer = LighterSigner(creds, base_url=base_url)
            print(f"Signer available: {signer.available()}")
            token = signer.create_auth_token(deadline_seconds=60)
            print(f"Auth token length: {len(token) if token else 0}")
        except Exception as exc:
            print(f"Signer init/auth error: {exc}")
            signer = None
    else:
        print("Signer creds not fully provided via env; skipping auth token test")

    # 3) Account endpoints
    # 3a) nextNonce (no auth required)
    if account_index_s and api_key_index_s:
        try:
            params = {
                'account_index': int(account_index_s, 0) if account_index_s.startswith('0x') else int(account_index_s),
                'api_key_index': int(api_key_index_s, 0) if api_key_index_s.startswith('0x') else int(api_key_index_s),
            }
            payload = _http_get_json(base_url, "/api/v1/nextNonce", params)
            next_nonce = payload.get('next_nonce') or payload.get('nonce')
            print(f"Account next_nonce: {next_nonce}")
        except Exception as exc:
            print(f"get_next_nonce error: {exc}")
    else:
        print("No account/api_key index provided; skipping next_nonce")

    # 3b) account state (requires auth)
    if account_index_s and token:
        try:
            params = {'by': 'index', 'value': int(account_index_s, 0) if account_index_s.startswith('0x') else int(account_index_s), 'auth': token}
            state = _http_get_json(base_url, "/api/v1/account", params)
            print(f"Account state keys: {list(state.keys())[:5]}")
        except Exception as exc:
            print(f"get_account_state error: {exc}")
    else:
        print("No auth token or account index; skipping account_state")

    # 4) Transaction send (optional; requires valid signer + token)
    if signer and token:
        try:
            # 发送一个无效的 tx_type/tx_info 以验证错误通路与鉴权（服务器应返回 code!=200）
            resp = _http_post_form_json(
                base_url,
                "/api/v1/sendTx",
                {
                    "tx_type": str(9999),
                    "tx_info": "{}",
                    "price_protection": "false",
                    "auth": token or "",
                },
            )
            print(f"send_tx response: {resp}")
        except Exception as exc:
            print(f"send_tx error: {exc}")
    else:
        print("No signer/token; skipping send_tx")


if __name__ == "__main__":
    asyncio.run(main())
