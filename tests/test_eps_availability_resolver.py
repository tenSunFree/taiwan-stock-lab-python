import datetime as dt

from app.ingestion.eps_availability_resolver import (
    EpsAvailabilitySource,
    ResolvedCumulativeEpsPoint,
    ResolvedEpsAvailability,
    build_resolved_cumulative_eps_point,
    resolve_eps_availability,
)
from app.ingestion.eps_mapper import RawCumulativeEps


def test_resolve_prefers_first_seen_at_when_present():
    result = resolve_eps_availability(
        first_seen_at=dt.date(2026, 8, 11),
        batch_report_date=dt.date(2026, 8, 31),
    )
    assert result == ResolvedEpsAvailability(
        available_at=dt.date(2026, 8, 11),
        source=EpsAvailabilitySource.FIRST_SEEN,
    )


def test_resolve_falls_back_to_batch_report_date_when_no_first_seen():
    result = resolve_eps_availability(
        first_seen_at=None,
        batch_report_date=dt.date(2026, 8, 31),
    )
    assert result == ResolvedEpsAvailability(
        available_at=dt.date(2026, 8, 31),
        source=EpsAvailabilitySource.BATCH_REPORT_DATE,
    )


def test_resolve_never_uses_a_synthetic_third_fallback():
    """
    Guardrail test: this project deliberately does NOT invent a
    statutory-deadline estimate when both real signals happen to
    coincide or when batch_report_date is the only one available —
    there is no third code path here to accidentally exercise. This
    test exists to make it obvious in review if such a path is ever
    added: resolve_eps_availability's source is always exactly one of
    the two real, observed enum values, never anything else.
    """
    result = resolve_eps_availability(
        first_seen_at=None,
        batch_report_date=dt.date(2026, 8, 31),
    )
    assert result.source in (
        EpsAvailabilitySource.FIRST_SEEN,
        EpsAvailabilitySource.BATCH_REPORT_DATE,
    )


def test_build_resolved_cumulative_eps_point_uses_first_seen_at_when_available():
    raw = RawCumulativeEps(
        stock_id="1101",
        fiscal_year=2026,
        quarter=2,
        cumulative_eps=0.38,
        batch_report_date=dt.date(2026, 8, 31),
    )
    point = build_resolved_cumulative_eps_point(
        raw=raw, first_seen_at=dt.date(2026, 8, 11)
    )
    assert point == ResolvedCumulativeEpsPoint(
        fiscal_year=2026,
        quarter=2,
        cumulative_eps=0.38,
        available_at=dt.date(2026, 8, 11),
    )


def test_build_resolved_cumulative_eps_point_falls_back_to_batch_report_date():
    """
    Historical-quarter case: this project has no first_seen_at
    observation for a quarter that was already old by the time daily
    ingestion started, so it must fall back to the coarser but still
    look-ahead-safe batch_report_date, not silently drop the point or
    invent a date.
    """
    raw = RawCumulativeEps(
        stock_id="1101",
        fiscal_year=2025,
        quarter=2,
        cumulative_eps=0.31,
        batch_report_date=dt.date(2025, 8, 29),
    )
    point = build_resolved_cumulative_eps_point(raw=raw, first_seen_at=None)
    assert point == ResolvedCumulativeEpsPoint(
        fiscal_year=2025,
        quarter=2,
        cumulative_eps=0.31,
        available_at=dt.date(2025, 8, 29),
    )


def test_build_resolved_cumulative_eps_point_preserves_stock_identity_via_caller():
    """
    ResolvedCumulativeEpsPoint itself has no stock_id field, matching
    QuarterlyEpsPoint/MonthlyRevenuePoint's design (operates on a
    single stock's points at a time). The caller (the future
    daily_ranking.py wiring) is responsible for keeping points grouped
    per stock_id before calling downstream functions.
    """
    raw = RawCumulativeEps(
        stock_id="1101",
        fiscal_year=2026,
        quarter=2,
        cumulative_eps=0.38,
        batch_report_date=dt.date(2026, 8, 31),
    )
    point = build_resolved_cumulative_eps_point(raw=raw, first_seen_at=None)
    assert not hasattr(point, "stock_id")
