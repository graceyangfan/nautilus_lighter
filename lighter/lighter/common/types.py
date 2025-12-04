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

from __future__ import annotations

import pyarrow as pa

from nautilus_trader.core.data import Data
from nautilus_trader.model.identifiers import InstrumentId


class LighterMarketStats(Data):
    """
    Lighter aggregated market stats event (CustomData payload).

    Fields mirror the public WS `market_stats` payload; numeric values are
    represented as floats for simplicity and portability.
    """

    def __init__(
        self,
        instrument_id: InstrumentId,
        ts_event: int,
        ts_init: int,
        index_price: float | None,
        mark_price: float | None,
        open_interest: float | None,
        last_trade_price: float | None = None,
        current_funding_rate: float | None = None,
        funding_rate: float | None = None,
        funding_timestamp: int | None = None,
        daily_base_token_volume: float | None = None,
        daily_quote_token_volume: float | None = None,
        daily_price_low: float | None = None,
        daily_price_high: float | None = None,
        daily_price_change: float | None = None,
    ) -> None:
        self.instrument_id = instrument_id
        self.index_price = index_price
        self.mark_price = mark_price
        self.open_interest = open_interest
        self.last_trade_price = last_trade_price
        self.current_funding_rate = current_funding_rate
        self.funding_rate = funding_rate
        self.funding_timestamp = funding_timestamp
        self.daily_base_token_volume = daily_base_token_volume
        self.daily_quote_token_volume = daily_quote_token_volume
        self.daily_price_low = daily_price_low
        self.daily_price_high = daily_price_high
        self.daily_price_change = daily_price_change
        self._ts_event = ts_event
        self._ts_init = ts_init

    def __eq__(self, other: object) -> bool:
        return isinstance(other, LighterMarketStats) and self.instrument_id == other.instrument_id

    def __hash__(self) -> int:
        return hash(self.instrument_id)

    @property
    def ts_event(self) -> int:
        return self._ts_event

    @property
    def ts_init(self) -> int:
        return self._ts_init

    @classmethod
    def schema(cls) -> pa.Schema:
        return pa.schema(
            {
                "instrument_id": pa.dictionary(pa.int8(), pa.string()),
                "ts_event": pa.uint64(),
                "ts_init": pa.uint64(),
                "index_price": pa.float64(),
                "mark_price": pa.float64(),
                "open_interest": pa.float64(),
                "last_trade_price": pa.float64(),
                "current_funding_rate": pa.float64(),
                "funding_rate": pa.float64(),
                "funding_timestamp": pa.uint64(),
                "daily_base_token_volume": pa.float64(),
                "daily_quote_token_volume": pa.float64(),
                "daily_price_low": pa.float64(),
                "daily_price_high": pa.float64(),
                "daily_price_change": pa.float64(),
            },
            metadata={"type": "LighterMarketStats"},
        )

    @classmethod
    def from_dict(cls, values: dict) -> LighterMarketStats:
        return cls(
            instrument_id=InstrumentId.from_str(values["instrument_id"]),
            ts_event=values["ts_event"],
            ts_init=values["ts_init"],
            index_price=values.get("index_price"),
            mark_price=values.get("mark_price"),
            open_interest=values.get("open_interest"),
            last_trade_price=values.get("last_trade_price"),
            current_funding_rate=values.get("current_funding_rate"),
            funding_rate=values.get("funding_rate"),
            funding_timestamp=values.get("funding_timestamp"),
            daily_base_token_volume=values.get("daily_base_token_volume"),
            daily_quote_token_volume=values.get("daily_quote_token_volume"),
            daily_price_low=values.get("daily_price_low"),
            daily_price_high=values.get("daily_price_high"),
            daily_price_change=values.get("daily_price_change"),
        )

    @staticmethod
    def to_dict(obj: LighterMarketStats) -> dict:
        return {
            "type": type(obj).__name__,
            "instrument_id": obj.instrument_id.value,
            "ts_event": obj._ts_event,
            "ts_init": obj._ts_init,
            "index_price": obj.index_price,
            "mark_price": obj.mark_price,
            "open_interest": obj.open_interest,
            "last_trade_price": obj.last_trade_price,
            "current_funding_rate": obj.current_funding_rate,
            "funding_rate": obj.funding_rate,
            "funding_timestamp": obj.funding_timestamp,
            "daily_base_token_volume": obj.daily_base_token_volume,
            "daily_quote_token_volume": obj.daily_quote_token_volume,
            "daily_price_low": obj.daily_price_low,
            "daily_price_high": obj.daily_price_high,
            "daily_price_change": obj.daily_price_change,
        }

