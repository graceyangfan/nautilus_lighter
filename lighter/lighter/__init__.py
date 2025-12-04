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
"""
Lighter perpetual DEX adapter (instruments only; WIP).

This subpackage exposes the instrument provider and constants required to
load Lighter market metadata into Nautilus instruments.
"""

from nautilus_trader.adapters.lighter.common.constants import LIGHTER
from nautilus_trader.adapters.lighter.common.constants import LIGHTER_VENUE
from nautilus_trader.adapters.lighter.providers import LighterInstrumentProvider
from nautilus_trader.adapters.lighter.common.types import LighterMarketStats
from nautilus_trader.adapters.lighter.data import LighterDataClient
from nautilus_trader.adapters.lighter.config import LighterDataClientConfig, LighterExecClientConfig
from nautilus_trader.adapters.lighter.factories import LighterLiveExecClientFactory
from nautilus_trader.adapters.lighter.factories import LighterLiveDataClientFactory

__all__ = [
    "LIGHTER",
    "LIGHTER_VENUE",
    "LighterInstrumentProvider",
    "LighterMarketStats",
    "LighterDataClient",
    "LighterDataClientConfig",
    "LighterExecClientConfig",
    "LighterLiveExecClientFactory",
    "LighterLiveDataClientFactory",
]
