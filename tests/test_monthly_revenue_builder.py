import datetime as dt

import pytest

from app.domain.monthly_revenue_builder import MonthlyRevenuePoint, build_revenue_yoy

TARGET_DATE = dt.date(2026, 8, 15)


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
