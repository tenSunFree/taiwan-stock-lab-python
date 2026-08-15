"""
Pure, provider-independent monthly-revenue YoY calculation.

DESIGN DECISION — availability vs. revenue period: "which month a
revenue figure belongs to" is NOT the same question as "when the
strategy could have known this figure." FinMind's TaiwanStockMonthRevenue
rows carry create_time (populated starting 2026-05-22; older rows may
have an empty create_time). This module treats a revenue point as
usable for the NUMERATOR only when its own available_at is known and
is on or before target_date — never inferred from the calendar month
alone. This avoids look-ahead bias: a company that discloses July
revenue on 2026/08/12 must not have that figure used for a
target_date of 2026/08/10, even though "July" itself already ended.

DESIGN DECISION — previous-year denominator: the comparison year's
same-month revenue is, in practice, always already disclosed by the
time this pipeline looks at it (it refers to a period well over a
year old). Rows without availability metadata (legacy rows predating
create_time) are therefore allowed as the DENOMINATOR only, never as
the numerator — using them as the "current" figure would risk
look-ahead bias for figures we genuinely cannot date; using them as
the "previous year" figure carries no such risk given how old they
necessarily are.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class MonthlyRevenuePoint:
    revenue_year: int
    revenue_month: int
    revenue: float
    available_at: dt.date | None


def build_revenue_yoy(
    *,
    target_date: dt.date,
    points: list[MonthlyRevenuePoint],
) -> float | None:
    """
    Year-over-year revenue growth using the newest revenue month known
    to be available as of target_date.

    Returns None if no eligible current-month point exists, if the
    matching previous-year month is missing, or if the previous
    year's revenue is not positive (division would be meaningless).
    """
    available_current_points = [
        point
        for point in points
        if point.available_at is not None
        and point.available_at <= target_date
        and point.revenue >= 0
    ]

    if not available_current_points:
        return None

    current = max(
        available_current_points,
        key=lambda point: (point.revenue_year, point.revenue_month, point.available_at),
    )

    previous_year_points = [
        point
        for point in points
        if point.revenue_year == current.revenue_year - 1
        and point.revenue_month == current.revenue_month
        and point.revenue >= 0
    ]

    if not previous_year_points:
        return None

    # Normally one row exists per stock/month. If multiple revisions
    # exist, prefer the latest known revision; legacy rows without
    # availability metadata sort last as a fallback, not a first pick.
    previous = max(
        previous_year_points,
        key=lambda point: (point.available_at or dt.date.min),
    )

    if previous.revenue <= 0:
        return None

    return current.revenue / previous.revenue - 1.0
