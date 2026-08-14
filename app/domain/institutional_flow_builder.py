"""
Pure, provider-independent institutional net-buy-ratio calculation.

DESIGN DECISION — units: FinMind's InstitutionalInvestorsBuySell
dataset reports buy/sell in SHARES, not currency amount. This module
defines the ratio entirely in shares: net institutional buy shares
over a trailing window, divided by total market volume (shares) over
the same window. Both numerator and denominator share the same unit.

DESIGN DECISION — as-of data / no look-ahead: FinMind's institutional
data updates around 20:00 on trading days — well after this
project's ~16:17-17:17 scheduled run time. target_date's own
institutional flow is NEVER available when the job runs, and this
module defensively excludes trading days on or after target_date
regardless of what the caller passes in.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

INSTITUTIONAL_TRAILING_WINDOW = 5


@dataclass(frozen=True)
class InstitutionalFlowPoint:
    trading_date: dt.date
    net_shares: int


def build_institutional_net_buy_ratio(
    *,
    target_date: dt.date,
    flow_points: list[InstitutionalFlowPoint],
    volume_by_date: dict[dt.date, float],
    window: int = INSTITUTIONAL_TRAILING_WINDOW,
) -> float | None:
    """
    Calculate trailing institutional net-buy shares divided by total
    stock trading volume over the exact same trading sessions.

    volume_by_date: total market volume (shares) for each trading
    day, typically built from the same historical price series
    already fetched in Step 8A (HistoricalPricePoint.volume) —
    reused here rather than fetched again.
    """
    if window <= 0:
        raise ValueError("window must be positive")

    valid_points = sorted(
        (point for point in flow_points if point.trading_date < target_date),
        key=lambda point: point.trading_date,
    )

    if len(valid_points) < window:
        return None

    trailing = valid_points[-window:]

    total_volume = 0.0
    for point in trailing:
        volume = volume_by_date.get(point.trading_date)
        if volume is None or volume <= 0:
            return None
        total_volume += volume

    if total_volume <= 0:
        return None

    total_net_shares = sum(point.net_shares for point in trailing)
    return float(total_net_shares) / total_volume
