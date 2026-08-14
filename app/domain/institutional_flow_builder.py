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

    The required trailing sessions are defined by the historical-price
    volume series, not by whichever institutional-flow rows happen to
    be available.

    If any required session lacks institutional data, the result is
    None rather than silently substituting an older flow session.
    """

    if window <= 0:
        raise ValueError("window must be positive")

    flow_by_date: dict[
        dt.date,
        int,
    ] = {}

    for point in flow_points:
        if point.trading_date >= target_date:
            continue

        # Mapper output should contain one aggregated point per date.
        # Duplicates indicate an invalid domain input.
        if point.trading_date in flow_by_date:
            return None

        flow_by_date[point.trading_date] = point.net_shares

    eligible_volume_dates = sorted(
        trading_date
        for trading_date, volume in volume_by_date.items()
        if (trading_date < target_date and volume > 0)
    )

    if len(eligible_volume_dates) < window:
        return None

    trailing_dates = eligible_volume_dates[-window:]

    total_net_shares = 0
    total_volume = 0.0

    for trading_date in trailing_dates:
        net_shares = flow_by_date.get(trading_date)

        volume = volume_by_date.get(trading_date)

        if net_shares is None:
            return None

        if volume is None or volume <= 0:
            return None

        total_net_shares += net_shares

        total_volume += volume

    if total_volume <= 0:
        return None

    return float(total_net_shares) / total_volume
