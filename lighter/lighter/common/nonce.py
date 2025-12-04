from __future__ import annotations

# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------

import asyncio
"""Nonce management for Lighter API keys.

The manager tracks a per-API-key nonce locally. It fetches the initial value
via a supplied coroutine, increments locally for each signed transaction, and
supports rollback + refresh on error.
"""

from dataclasses import dataclass
from typing import Awaitable, Callable


@dataclass
class _KeyState:
    """Internal state per API key (nonce value + lock)."""

    value: int
    lock: asyncio.Lock


class NonceManager:
    """Lightweight per-API-key nonce manager.

    The execution client provides the ``fetcher`` coroutine; the manager ensures
    a nonce is available for the given API key and advances it safely.
    """

    def __init__(self) -> None:
        self._states: dict[int, _KeyState] = {}

    async def ensure(self, api_key_index: int, fetcher: Callable[[int], Awaitable[int]]) -> None:
        """Ensure there is a cached nonce value for the API key."""
        if api_key_index in self._states:
            return
        initial = await fetcher(api_key_index)
        self._states[api_key_index] = _KeyState(value=int(initial), lock=asyncio.Lock())

    async def next(self, api_key_index: int, fetcher: Callable[[int], Awaitable[int]]) -> int:
        """Return the next nonce for the API key and increment the local counter."""
        await self.ensure(api_key_index, fetcher)
        st = self._states[api_key_index]
        async with st.lock:
            v = st.value
            st.value += 1
            return v

    async def rollback_and_refresh(self, api_key_index: int, fetcher: Callable[[int], Awaitable[int]]) -> int:
        """Refresh the nonce by fetching a new value from the venue."""
        st = self._states.get(api_key_index)
        if st is None:
            await self.ensure(api_key_index, fetcher)
            return self._states[api_key_index].value
        async with st.lock:
            fresh = await fetcher(api_key_index)
            st.value = int(fresh)
            return st.value
