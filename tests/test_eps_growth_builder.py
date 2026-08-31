import datetime as dt

import pytest

from app.domain.eps_growth_builder import (
    QuarterlyEpsPoint,
    build_eps_growth_sustained_signal,
    build_eps_yoy,
    combine_fundamental_growth_signal,
)

TARGET_DATE = dt.date(2026, 8, 15)


def _point(
    fiscal_year: int, quarter: int, eps: float, available_at: dt.date
) -> QuarterlyEpsPoint:
    return QuarterlyEpsPoint(
        fiscal_year=fiscal_year, quarter=quarter, eps=eps, available_at=available_at
    )


# --- QuarterlyEpsPoint validation --------------------------------------------


def test_quarter_must_be_1_to_4():
    with pytest.raises(ValueError, match="quarter must be 1-4"):
        QuarterlyEpsPoint(
            fiscal_year=2026, quarter=5, eps=1.0, available_at=dt.date(2026, 5, 15)
        )


def test_quarter_zero_rejected():
    with pytest.raises(ValueError, match="quarter must be 1-4"):
        QuarterlyEpsPoint(
            fiscal_year=2026, quarter=0, eps=1.0, available_at=dt.date(2026, 5, 15)
        )


# --- build_eps_yoy ------------------------------------------------------------


def test_no_points_returns_none():
    assert build_eps_yoy(target_date=TARGET_DATE, points=[]) is None


def test_current_quarter_not_yet_available_returns_none():
    points = [
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 6.0, dt.date(2026, 8, 20)),  # after TARGET_DATE
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) is None


def test_missing_previous_year_same_quarter_returns_none():
    points = [
        _point(2026, 2, 6.0, dt.date(2026, 8, 10)),
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) is None


def test_zero_previous_year_eps_returns_none():
    points = [
        _point(2025, 2, 0.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 6.0, dt.date(2026, 8, 10)),
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) is None


def test_negative_previous_year_eps_returns_none_not_a_sign_flip_percentage():
    """
    A loss-to-profit swing (previous EPS negative, current EPS
    positive) must NOT be computed as a numerically well-formed but
    economically meaningless percentage (e.g. 1 / -1 - 1 = -200%,
    which reads as "200% worse" despite representing an improvement).
    """
    points = [
        _point(2025, 2, -1.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 1.0, dt.date(2026, 8, 10)),
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) is None


def test_latest_available_quarter_is_used():
    points = [
        _point(2025, 1, 4.0, dt.date(2025, 5, 10)),
        _point(2026, 1, 4.4, dt.date(2026, 5, 10)),
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 6.0, dt.date(2026, 8, 10)),
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) == pytest.approx(0.20)


def test_future_dated_current_quarter_excluded_even_if_quarter_already_passed():
    points = [
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 999.0, dt.date(2026, 9, 1)),  # after TARGET_DATE
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) is None


def test_future_dated_previous_year_revision_excluded():
    """
    Unlike monthly_revenue_builder's original bug, this must be
    correct from the outset: a previous-year point dated AFTER
    target_date must be excluded, even though there is no "undated
    legacy row" fallback to reach for instead — this source is always
    dated, so the denominator is simply unresolvable, and the result
    is None, not a computation against not-yet-public data.
    """
    points = [
        _point(2025, 2, 5.0, dt.date(2026, 9, 1)),  # revision, after TARGET_DATE
        _point(2026, 2, 6.0, dt.date(2026, 8, 10)),
    ]
    assert build_eps_yoy(target_date=TARGET_DATE, points=points) is None


# --- build_eps_growth_sustained_signal ---------------------------------------


def test_sustained_fewer_than_window_quarters_returns_none():
    # Only one quarter (2026Q2) available — window is 2.
    points = [
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 6.0, dt.date(2026, 8, 10)),
    ]
    assert (
        build_eps_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is None
    )


def test_sustained_both_quarters_pass_returns_true():
    # 2026Q1 +22%, 2026Q2 +15% — both clear 10%, latest (Q2) too.
    points = [
        _point(2025, 1, 4.0, dt.date(2025, 5, 10)),
        _point(2026, 1, 4.88, dt.date(2026, 5, 10)),  # +22%
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 5.75, dt.date(2026, 8, 10)),  # +15%
    ]
    assert (
        build_eps_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is True
    )


def test_sustained_latest_quarter_below_threshold_returns_false():
    # 2026Q1 +22% (passes), 2026Q2 +5% (fails) — latest quarter itself
    # must clear 10%; it doesn't, so not "sustained" even though the
    # older quarter passed.
    points = [
        _point(2025, 1, 4.0, dt.date(2025, 5, 10)),
        _point(2026, 1, 4.88, dt.date(2026, 5, 10)),  # +22%
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 5.25, dt.date(2026, 8, 10)),  # +5%
    ]
    assert (
        build_eps_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is False
    )


def test_sustained_older_quarter_below_threshold_returns_false():
    # With a 2-of-2 window, BOTH quarters must pass — unlike revenue's
    # "allow one soft month," a single soft quarter here does break
    # the sustained reading (see module docstring for why quarterly
    # data doesn't need that leniency).
    points = [
        _point(2025, 1, 4.0, dt.date(2025, 5, 10)),
        _point(2026, 1, 4.2, dt.date(2026, 5, 10)),  # +5% (fails)
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 5.75, dt.date(2026, 8, 10)),  # +15% (latest, passes)
    ]
    assert (
        build_eps_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is False
    )


def test_sustained_missing_previous_year_quarter_returns_none_not_best_effort():
    points = [
        _point(2026, 1, 4.88, dt.date(2026, 5, 10)),
        _point(2025, 2, 5.0, dt.date(2025, 8, 10)),
        _point(2026, 2, 5.75, dt.date(2026, 8, 10)),
        # 2025 Q1 (denominator for 2026 Q1) is entirely missing.
    ]
    assert (
        build_eps_growth_sustained_signal(target_date=TARGET_DATE, points=points)
        is None
    )


def test_sustained_gap_quarter_inside_window_returns_none_not_bridged():
    """
    Same regression class as monthly_revenue_builder's gap-month bug,
    applied to quarters: the window must walk back STRICTLY
    CONSECUTIVE quarters from the latest available quarter. Latest is
    2026Q3; 2026Q2 is entirely missing, while 2026Q1 (older) has data.
    A buggy "whichever N quarters have data" implementation would
    wrongly bridge the gap and use [2026Q3, 2026Q1] instead of the
    required [2026Q3, 2026Q2] — this must instead return None.
    """
    points = [
        _point(2025, 1, 4.0, dt.date(2025, 5, 10)),
        _point(2026, 1, 4.8, dt.date(2026, 5, 10)),  # +20%, but NOT in window
        # 2026 Q2 missing entirely (the gap)
        _point(2025, 3, 5.0, dt.date(2025, 11, 10)),
        _point(2026, 3, 6.0, dt.date(2026, 11, 10)),  # +20%, latest
    ]
    assert (
        build_eps_growth_sustained_signal(
            target_date=dt.date(2026, 12, 1), points=points
        )
        is None
    )


def test_sustained_handles_fiscal_year_boundary():
    # Latest available quarter is 2026 Q1. Walking back one quarter
    # must roll the fiscal year backward: 2026Q1 -> 2025Q4.
    points = [
        _point(2024, 4, 4.0, dt.date(2025, 3, 20)),
        _point(2025, 4, 4.6, dt.date(2026, 3, 20)),  # +15%
        _point(2025, 1, 4.0, dt.date(2025, 5, 10)),
        _point(2026, 1, 4.8, dt.date(2026, 5, 10)),  # +20% (latest)
    ]
    assert (
        build_eps_growth_sustained_signal(
            target_date=dt.date(2026, 6, 1), points=points
        )
        is True
    )


def test_sustained_invalid_window_quarters_raises():
    with pytest.raises(ValueError, match="window_quarters must be positive"):
        build_eps_growth_sustained_signal(
            target_date=TARGET_DATE, points=[], window_quarters=0
        )


def test_sustained_invalid_min_pass_quarters_raises():
    with pytest.raises(ValueError, match="min_pass_quarters must be between"):
        build_eps_growth_sustained_signal(
            target_date=TARGET_DATE, points=[], min_pass_quarters=3
        )


# --- combine_fundamental_growth_signal (tri-state OR) ------------------------


@pytest.mark.parametrize(
    "revenue,eps,expected",
    [
        (True, True, True),
        (True, False, True),
        (True, None, True),
        (False, True, True),
        (None, True, True),
        (False, False, False),
        (False, None, None),
        (None, False, None),
        (None, None, None),
    ],
)
def test_combine_truth_table(revenue, eps, expected):
    assert combine_fundamental_growth_signal(revenue=revenue, eps=eps) is expected
