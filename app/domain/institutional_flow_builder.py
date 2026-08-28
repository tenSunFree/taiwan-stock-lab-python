"""
Pure, provider-independent institutional net-buy calculations.

This module exposes two independent signals built from the same
InstitutionalFlowPoint input:

- build_institutional_net_buy_ratio: a 5-session trailing net-buy /
  volume RATIO, used as the "institutional" SCORING FACTOR (see
  app.domain.scoring's FACTOR_WEIGHTS).
- build_institutional_net_buy_positive: a simple 3-session trailing
  net-buy SIGN check (> 0 or not), used purely as a DISPLAY signal in
  the report (see app.reports.text_renderer's "法人籌碼" block) — it
  is not a scoring factor and does not affect FACTOR_WEIGHTS or the
  ratio above in any way.

DESIGN DECISION — units: FinMind's InstitutionalInvestorsBuySell
dataset reports buy/sell in SHARES, not currency amount. This module
defines both calculations entirely in shares: net institutional buy
shares over a trailing window (optionally divided by total market
volume in shares over the same window for the ratio). Numerator and
denominator always share the same unit.

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


INSTITUTIONAL_NET_BUY_POSITIVE_WINDOW = 3


def build_institutional_net_buy_positive(
    *,
    target_date: dt.date,
    flow_points: list[InstitutionalFlowPoint],
    volume_by_date: dict[dt.date, float],
    window: int = INSTITUTIONAL_NET_BUY_POSITIVE_WINDOW,
) -> bool | None:
    """
    Whether cumulative institutional net-buy shares over the trailing
    `window` trading sessions (default 3) is strictly positive.

    This answers a DIFFERENT question from
    build_institutional_net_buy_ratio above. That function computes a
    5-session net-buy/volume RATIO used as a scoring factor (see
    rule-v1.2.0's FACTOR_WEIGHTS, "institutional" key). This function
    is a simple yes/no display signal over a shorter, fixed 3-session
    window ("did institutions net-buy over the last 3 sessions, in
    aggregate, at all") — it is NOT a scoring factor, does not feed
    into FACTOR_WEIGHTS or bounded_momentum_score-style normalization,
    and does not change the 5-day ratio's own value or meaning.

    Same design rules as build_institutional_net_buy_ratio:
    - no look-ahead: sessions on/after target_date are excluded
      regardless of what flow_points/volume_by_date contain.
    - the required trailing sessions are defined by the
      historical-price volume series, not by whichever
      institutional-flow rows happen to be available.
    - if ANY required session lacks institutional data, the result is
      None rather than silently computing from a partial window.
    """
    if window <= 0:
        raise ValueError("window must be positive")

    flow_by_date: dict[dt.date, int] = {}

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

    for trading_date in trailing_dates:
        net_shares = flow_by_date.get(trading_date)

        if net_shares is None:
            return None

        total_net_shares += net_shares

    return total_net_shares > 0
