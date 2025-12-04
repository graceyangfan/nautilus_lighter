from __future__ import annotations

from typing import Any, Iterable, Set


def parse_hex_int(value: Any) -> int | None:
    """Parse an integer from int/str, supporting 0x-prefixed hex.

    Returns None on failure instead of raising.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        s = str(value).strip()
        return int(s, 16) if s.lower().startswith("0x") else int(s)
    except (TypeError, ValueError):
        return None


def normalize_filter_values(value: Any, to_lower: bool = False) -> Set[str]:
    """Normalize filter values to a case-unified set of strings.

    Accepts a single string or an iterable of strings; ignores non-str items.
    """
    if value is None:
        return set()
    if isinstance(value, str):
        values: Iterable[str] = [value]
    else:
        try:
            values = list(value)  # type: ignore[assignment]
        except TypeError:
            values = []
    return {
        (item.lower() if to_lower else item.upper())
        for item in values
        if isinstance(item, str)
    }


def is_nonce_error(err: object) -> bool:
    """Heuristic detector for nonce-related error messages from server responses.

    Accepts strings or dict-like objects (with 'error'/'message' keys). Returns True
    if the textual content indicates a nonce mismatch/invalid nonce.
    """
    try:
        text = None
        if isinstance(err, dict):
            text = err.get("error") or err.get("message") or err.get("reason")
        if text is None and err is not None:
            text = str(err)
        if not isinstance(text, str):
            return False
        lower = text.lower()
        return ("invalid nonce" in lower) or ("nonce" in lower and "invalid" in lower or "mismatch" in lower)
    except Exception:
        return False
