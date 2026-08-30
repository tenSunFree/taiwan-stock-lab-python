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

DESIGN DECISION — build_revenue_growth_sustained_signal(): this is a
SEPARATE, DISPLAY-ONLY signal from build_revenue_yoy() above, for the
report's "基本面" block (see app.reports.text_renderer). It answers a
different question than build_revenue_yoy() (which looks at only the
single newest available month, feeding the "fundamental" SCORING
FACTOR): "has revenue growth been sustained over the last few
months," not just "what is the latest YoY number." Rule (v1, revenue
only — EPS is a known data gap, see this module's module-level
comments and StockFeatures.fundamental_growth_sustained):

    latest_yoy >= REVENUE_GROWTH_THRESHOLD
    and count(yoy >= REVENUE_GROWTH_THRESHOLD for the last
        REVENUE_GROWTH_WINDOW_MONTHS STRICTLY CONSECUTIVE calendar
        months) >= REVENUE_GROWTH_MIN_PASS_MONTHS

Deliberately NOT "every one of the last N months must individually
clear the threshold" — monthly revenue is noisy (shipment timing,
working-day counts, Lunar New Year, order-recognition timing), so a
single soft month should not by itself erase an otherwise-sustained
growth trend. But the LATEST month is still required to individually
clear the bar, so a company whose growth already trails off this
month is not marked "sustained" purely on the strength of older
months.

"STRICTLY CONSECUTIVE" is load-bearing: the window is built by walking
back one calendar month at a time from the latest available month
(e.g. 07 -> 06 -> 05), never by taking "whichever 3 calendar months
happen to have data." A gap month inside the window (say 05 is
entirely missing while 07/06/04 all have rows) must NOT be silently
bridged by reaching further back to 04 — that would compute a
"3-month window" that isn't actually 3 consecutive months, quietly
weakening the very persistence claim this signal exists to make.

Same no-look-ahead / no-best-effort philosophy as build_revenue_yoy():
each of the REVENUE_GROWTH_WINDOW_MONTHS required consecutive
calendar months must independently resolve to a YoY value (an
eligible available_at <= target_date current-side point AND a
resolvable previous-year same-month point). Any single missing or
unresolvable required month — including a gap inside the window —
makes the whole signal None ("data insufficient") rather than
silently scoring on a partial or non-consecutive window — the same
tri-state convention used everywhere else in this project
(institutional_net_buy_3d_positive, technical_low_with_rising_signal,
risk_quality_raw, ...).
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


REVENUE_GROWTH_THRESHOLD = 0.10
REVENUE_GROWTH_WINDOW_MONTHS = 3
REVENUE_GROWTH_MIN_PASS_MONTHS = 2


def _previous_calendar_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _build_trailing_calendar_months(
    *, latest_year: int, latest_month: int, window_months: int
) -> list[tuple[int, int]]:
    """
    Return `window_months` STRICTLY CONSECUTIVE calendar months, newest
    first, walking backward one calendar month at a time from
    (latest_year, latest_month) — e.g. latest=2026/07, window=3 ->
    [(2026, 7), (2026, 6), (2026, 5)]. Correctly rolls over the year
    boundary (2026/01 -> 2025/12). This does NOT look at which months
    actually have data — that check happens per-month in
    _resolve_month_yoy — so a data gap produces a required month with
    no matching point, not a silently skipped one.
    """
    result: list[tuple[int, int]] = []
    year, month = latest_year, latest_month
    for _ in range(window_months):
        result.append((year, month))
        year, month = _previous_calendar_month(year, month)
    return result


def _resolve_month_yoy(
    *,
    target_date: dt.date,
    revenue_year: int,
    revenue_month: int,
    points: list[MonthlyRevenuePoint],
) -> float | None:
    """
    Resolve a single calendar month's YoY, applying the SAME
    no-look-ahead current-side filter (available_at <= target_date)
    that build_revenue_yoy applies — this is evaluated independently
    per required calendar month, not just for the single newest month,
    so a required month with no eligible current-side point resolves
    to None here rather than silently being skipped by the caller.
    """
    current_points = [
        point
        for point in points
        if point.revenue_year == revenue_year
        and point.revenue_month == revenue_month
        and point.available_at is not None
        and point.available_at <= target_date
        and point.revenue >= 0
    ]
    if not current_points:
        return None

    current = max(current_points, key=lambda point: point.available_at)

    previous_year_points = [
        point
        for point in points
        if point.revenue_year == revenue_year - 1
        and point.revenue_month == revenue_month
        and point.revenue >= 0
    ]
    if not previous_year_points:
        return None

    previous = max(
        previous_year_points,
        key=lambda point: (point.available_at or dt.date.min),
    )

    if previous.revenue <= 0:
        return None

    return current.revenue / previous.revenue - 1.0


def build_revenue_growth_sustained_signal(
    *,
    target_date: dt.date,
    points: list[MonthlyRevenuePoint],
    window_months: int = REVENUE_GROWTH_WINDOW_MONTHS,
    threshold: float = REVENUE_GROWTH_THRESHOLD,
    min_pass_months: int = REVENUE_GROWTH_MIN_PASS_MONTHS,
) -> bool | None:
    """
    Whether monthly revenue YoY growth has been "sustained" over the
    `window_months` STRICTLY CONSECUTIVE calendar months ending at the
    newest revenue month known to be available as of target_date (see
    this module's docstring for the full rule and rationale).

    "Strictly consecutive" matters: this walks back one calendar month
    at a time from the latest available month (e.g. 07 -> 06 -> 05),
    it does NOT simply take whichever 3 calendar months happen to have
    data. A gap month inside the window (say 05 is entirely missing
    while 07/06/04 have rows) must NOT be silently bridged by reaching
    further back to 04 — that would compute a "3-month window" that
    isn't actually 3 consecutive months, quietly violating the
    persistence rule it's supposed to measure. Such a gap makes the
    whole signal None, exactly like any other unresolvable required
    month.

    Returns None (data insufficient) when no eligible current-month
    point exists at all, or when ANY of the `window_months` required
    CONSECUTIVE calendar months cannot be resolved (missing current-
    side point, missing/non-positive previous-year same-month revenue)
    — never computed from a partial or non-consecutive window.
    """
    if window_months <= 0:
        raise ValueError("window_months must be positive")
    if not (1 <= min_pass_months <= window_months):
        raise ValueError("min_pass_months must be between 1 and window_months")

    available_current_points = [
        point
        for point in points
        if point.available_at is not None
        and point.available_at <= target_date
        and point.revenue >= 0
    ]

    if not available_current_points:
        return None

    latest = max(
        available_current_points,
        key=lambda point: (point.revenue_year, point.revenue_month, point.available_at),
    )

    required_months = _build_trailing_calendar_months(
        latest_year=latest.revenue_year,
        latest_month=latest.revenue_month,
        window_months=window_months,
    )

    yoy_values: list[float] = []
    for revenue_year, revenue_month in required_months:
        yoy = _resolve_month_yoy(
            target_date=target_date,
            revenue_year=revenue_year,
            revenue_month=revenue_month,
            points=points,
        )
        if yoy is None:
            return None
        yoy_values.append(yoy)

    # required_months is newest-first, so yoy_values[0] is the latest
    # month's YoY.
    latest_yoy = yoy_values[0]
    pass_count = sum(1 for yoy in yoy_values if yoy >= threshold)

    return latest_yoy >= threshold and pass_count >= min_pass_months
