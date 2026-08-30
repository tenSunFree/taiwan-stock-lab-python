import datetime as dt

import pytest

from app.domain.monthly_revenue_builder import (
    MonthlyRevenuePoint,
    build_revenue_growth_sustained_signal,
    build_revenue_yoy,
)

TARGET_DATE = dt.date(2026, 8, 15)


def _point(year: int, month: int, revenue: float, available_at) -> MonthlyRevenuePoint:
    return MonthlyRevenuePoint(
        revenue_year=year,
        revenue_month=month,
        revenue=revenue,
        available_at=available_at,
    )


def test_no_points_returns_none():
    assert build_revenue_yoy(target_date=TARGET_DATE, points=[]) is None


def test_current_month_not_yet_available_returns_none():
    points = [
        MonthlyRevenuePoint(
            revenue_year=2025, revenue_month=7, revenue=100.0, available_at=None
        ),
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=7,
            revenue=120.0,
            available_at=dt.date(2026, 8, 20),
        ),
    ]
    assert build_revenue_yoy(target_date=TARGET_DATE, points=points) is None


def test_missing_previous_year_same_month_returns_none():
    points = [
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=7,
            revenue=120.0,
            available_at=dt.date(2026, 8, 10),
        ),
    ]
    assert build_revenue_yoy(target_date=TARGET_DATE, points=points) is None


def test_zero_previous_year_revenue_returns_none():
    points = [
        MonthlyRevenuePoint(
            revenue_year=2025, revenue_month=7, revenue=0.0, available_at=None
        ),
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=7,
            revenue=120.0,
            available_at=dt.date(2026, 8, 10),
        ),
    ]
    assert build_revenue_yoy(target_date=TARGET_DATE, points=points) is None


def test_latest_available_month_is_used():
    points = [
        MonthlyRevenuePoint(
            revenue_year=2025, revenue_month=6, revenue=100.0, available_at=None
        ),
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=6,
            revenue=110.0,
            available_at=dt.date(2026, 7, 10),
        ),
        MonthlyRevenuePoint(
            revenue_year=2025, revenue_month=7, revenue=100.0, available_at=None
        ),
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=7,
            revenue=130.0,
            available_at=dt.date(2026, 8, 10),
        ),
    ]
    assert build_revenue_yoy(target_date=TARGET_DATE, points=points) == pytest.approx(
        0.30
    )


def test_future_dated_availability_excluded_even_if_month_already_passed():
    points = [
        MonthlyRevenuePoint(
            revenue_year=2025, revenue_month=7, revenue=100.0, available_at=None
        ),
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=7,
            revenue=999.0,
            available_at=dt.date(2026, 9, 1),
        ),
    ]
    assert build_revenue_yoy(target_date=TARGET_DATE, points=points) is None


def test_legacy_row_without_availability_usable_only_as_denominator():
    points = [
        MonthlyRevenuePoint(
            revenue_year=2025, revenue_month=7, revenue=100.0, available_at=None
        ),
        MonthlyRevenuePoint(
            revenue_year=2026,
            revenue_month=7,
            revenue=130.0,
            available_at=dt.date(2026, 8, 10),
        ),
    ]
    assert build_revenue_yoy(target_date=TARGET_DATE, points=points) == pytest.approx(
        0.30
    )


# --- build_revenue_growth_sustained_signal ----------------------------------


def test_sustained_fewer_than_window_months_returns_none():
    # Only May+June 2026 available (2 distinct months) — window is 3.
    points = [
        _point(2025, 5, 100.0, None),
        _point(2026, 5, 118.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 112.0, dt.date(2026, 7, 10)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is None
    )


def test_sustained_all_three_months_pass_returns_true():
    # May +18%, June +12%, July +15% — all 3 clear 10%, latest (July) too.
    points = [
        _point(2025, 5, 100.0, None),
        _point(2026, 5, 118.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 112.0, dt.date(2026, 7, 10)),
        _point(2025, 7, 100.0, None),
        _point(2026, 7, 115.0, dt.date(2026, 8, 10)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is True
    )


def test_sustained_one_soft_month_still_true_if_latest_and_two_of_three_pass():
    # July (latest) +15%, June +8% (soft/fails), May +22% — latest passes
    # and 2 of 3 months pass, so still "sustained" despite June's dip.
    points = [
        _point(2025, 5, 100.0, None),
        _point(2026, 5, 122.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 108.0, dt.date(2026, 7, 10)),
        _point(2025, 7, 100.0, None),
        _point(2026, 7, 115.0, dt.date(2026, 8, 10)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is True
    )


def test_sustained_only_one_of_three_months_pass_returns_false():
    # July (latest) +15%, June -5%, May +2% — latest passes but only 1 of
    # 3 months clears the bar, so not "sustained".
    points = [
        _point(2025, 5, 100.0, None),
        _point(2026, 5, 102.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 95.0, dt.date(2026, 7, 10)),
        _point(2025, 7, 100.0, None),
        _point(2026, 7, 115.0, dt.date(2026, 8, 10)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is False
    )


def test_sustained_latest_month_below_threshold_returns_false_even_if_others_pass():
    # July (latest) +8% (fails), June +20%, May +18% — 2 of 3 months
    # pass, but the LATEST month itself must also clear 10%; it doesn't.
    points = [
        _point(2025, 5, 100.0, None),
        _point(2026, 5, 118.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 120.0, dt.date(2026, 7, 10)),
        _point(2025, 7, 100.0, None),
        _point(2026, 7, 108.0, dt.date(2026, 8, 10)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is False
    )


def test_sustained_missing_previous_year_month_returns_none_not_best_effort():
    # May 2025 (previous-year denominator) is missing entirely — May's
    # YoY can't be resolved, so the WHOLE window is None, even though
    # June and July both resolve fine and would pass.
    points = [
        _point(2026, 5, 118.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 112.0, dt.date(2026, 7, 10)),
        _point(2025, 7, 100.0, None),
        _point(2026, 7, 115.0, dt.date(2026, 8, 10)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is None
    )


def test_sustained_future_dated_current_month_excluded():
    # July 2026 is not yet available as of TARGET_DATE — only May/June
    # are eligible, which is fewer than the 3-month window.
    points = [
        _point(2025, 5, 100.0, None),
        _point(2026, 5, 118.0, dt.date(2026, 6, 10)),
        _point(2025, 6, 100.0, None),
        _point(2026, 6, 112.0, dt.date(2026, 7, 10)),
        _point(2025, 7, 100.0, None),
        _point(2026, 7, 999.0, dt.date(2026, 9, 1)),
    ]
    assert (
        build_revenue_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is None
    )


def test_sustained_gap_month_inside_window_returns_none_not_bridged():
    """
    Regression test for a real bug in an earlier draft of this
    function: the window MUST be built by walking back STRICTLY
    CONSECUTIVE calendar months from the latest available month
    (07 -> 06 -> 05), never by taking "whichever 3 calendar months
    happen to have data".

    Here the latest available month is 2026/07. 2026/05 is entirely
    missing, while 2026/04 (an even older month) DOES have data. A
    buggy implementation that just picks "the 3 most recent months
    with any data" would wrongly skip the gap and use [07, 06, 04]
    (all +20%) -> True. The correct behavior is None: 05 is a
    required month in the 07/06/05 window and it's missing, so the
    window is incomplete — it must not be silently bridged by
    reaching past the gap to an older month.
    """
    points = [
        # 2026/07 +20%
        _point(2026, 7, 120.0, dt.date(2026, 8, 5)),
        _point(2025, 7, 100.0, None),
        # 2026/06 +20%
        _point(2026, 6, 120.0, dt.date(2026, 7, 5)),
        _point(2025, 6, 100.0, None),
        # 2026/05 — entirely missing (the gap)
        # 2026/04 +20% — has data, but is NOT part of the required
        # consecutive window (07/06/05) and must not be substituted in.
        _point(2026, 4, 120.0, dt.date(2026, 5, 5)),
        _point(2025, 4, 100.0, None),
    ]
    assert (
        build_revenue_growth_sustained_signal(
            target_date=dt.date(2026, 8, 7), points=points
        )
        is None
    )


def test_sustained_handles_year_boundary():
    # Latest available month is 2026/01. Walking back strictly
    # consecutive calendar months must roll over the year boundary:
    # 2026/01 -> 2025/12 -> 2025/11.
    points = [
        # 2026/01 +20% (latest)
        _point(2026, 1, 120.0, dt.date(2026, 2, 5)),
        _point(2025, 1, 100.0, None),
        # 2025/12 +15%
        _point(2025, 12, 115.0, dt.date(2026, 1, 5)),
        _point(2024, 12, 100.0, None),
        # 2025/11 +5% (soft, but latest + 1 of the other 2 still pass)
        _point(2025, 11, 105.0, dt.date(2025, 12, 5)),
        _point(2024, 11, 100.0, None),
    ]
    assert (
        build_revenue_growth_sustained_signal(
            target_date=dt.date(2026, 2, 10), points=points
        )
        is True
    )


def test_sustained_invalid_window_months_raises():
    with pytest.raises(ValueError, match="window_months must be positive"):
        build_revenue_growth_sustained_signal(
            target_date=TARGET_DATE, points=[], window_months=0
        )


def test_sustained_invalid_min_pass_months_raises():
    with pytest.raises(ValueError, match="min_pass_months must be between"):
        build_revenue_growth_sustained_signal(
            target_date=TARGET_DATE, points=[], min_pass_months=4
        )
