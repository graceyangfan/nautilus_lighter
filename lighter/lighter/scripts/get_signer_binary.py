#!/usr/bin/env python3
from __future__ import annotations

"""
Helper script to install the Lighter signer shared library for this adapter.

Usage examples
--------------

Copy from an existing local build (for example from `lighter-python-latest`):

  PYTHONPATH=/path/to/site-packages \\
  python adapters/lighter/scripts/get_signer_binary.py \\
    --src /tmp/lighter-python-latest/lighter/signers/lighter-signer-linux-amd64.so

Download from a custom URL (for example a release artifact you host):

  PYTHONPATH=/path/to/site-packages \\
  python adapters/lighter/scripts/get_signer_binary.py \\
    --url https://example.com/path/to/lighter-signer-linux-amd64.so

By default the script writes into the platform-specific file under:

  adapters/lighter/signers/

No secrets are required; this script does not read or print any API keys.
"""

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path
import platform


def _default_dest() -> Path:
    """Return a default signer path which matches LighterSigner expectations."""
    here = Path(__file__).resolve()
    base = here.parents[1] / "signers"
    sysname = platform.system()
    machine = platform.machine().lower()
    if sysname == "Darwin" and machine == "arm64":
        return base / "signer-arm64.dylib"
    if sysname == "Linux" and machine in ("x86_64", "amd64"):
        return base / "signer-amd64.so"
    if sysname == "Linux" and machine == "arm64":
        return base / "signer-arm64.so"
    if sysname == "Windows" and machine in ("x86_64", "amd64"):
        return base / "signer-amd64.dll"
    # Fallback to a generic name; user can override via --dest.
    return base / "signer-amd64.so"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the Lighter signer shared library for the adapter.",
    )
    parser.add_argument(
        "--src",
        type=str,
        help="Path to an existing signer .so file to copy from (mutually exclusive with --url).",
    )
    parser.add_argument(
        "--url",
        type=str,
        help="HTTP/HTTPS URL to download the signer .so from (mutually exclusive with --src).",
    )
    parser.add_argument(
        "--dest",
        type=str,
        default=str(_default_dest()),
        help="Destination path for the signer .so (default: platform-specific file under adapters/lighter/signers/).",
    )
    args = parser.parse_args()
    if bool(args.src) == bool(args.url):
        parser.error("Exactly one of --src or --url must be provided.")
    return args


def _ensure_dest_dir(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)


def _copy_from_src(src: Path, dest: Path) -> None:
    if not src.is_file():
        raise SystemExit(f"[signer] Source file does not exist: {src}")
    _ensure_dest_dir(dest)
    shutil.copy2(src, dest)
    print(f"[signer] Copied signer from {src} to {dest}")


def _download_from_url(url: str, dest: Path) -> None:
    _ensure_dest_dir(dest)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    print(f"[signer] Downloading signer from {url} ...")
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # nosec B310 (tooling script)
            data = resp.read()
    except Exception as exc:  # pragma: no cover - simple CLI tooling
        raise SystemExit(f"[signer] Download failed: {exc}")
    tmp.write_bytes(data)
    tmp.replace(dest)
    print(f"[signer] Downloaded signer to {dest} (size={dest.stat().st_size} bytes)")


def main() -> int:
    args = _parse_args()
    dest = Path(args.dest).resolve()
    if args.src:
        _copy_from_src(Path(args.src).expanduser().resolve(), dest)
    else:
        _download_from_url(args.url, dest)  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
