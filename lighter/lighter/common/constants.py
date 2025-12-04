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

"""Constants for the Lighter adapter (venue identifiers and base URLs)."""

from typing import Final

from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue


LIGHTER: Final[str] = "LIGHTER"
LIGHTER_VENUE: Final[Venue] = Venue(LIGHTER)
LIGHTER_CLIENT_ID: Final[ClientId] = ClientId(LIGHTER)

# Public REST base URLs (subject to change by operator)
LIGHTER_MAINNET_HTTP: Final[str] = "https://mainnet.zklighter.elliot.ai"
LIGHTER_TESTNET_HTTP: Final[str] = "https://testnet.zklighter.elliot.ai"

