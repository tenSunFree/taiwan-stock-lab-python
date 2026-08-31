import datetime as dt

import pytest

from app.domain.eps_growth_builder import QuarterlyEpsPoint
from app.ingestion.eps_availability_resolver import ResolvedCumulativeEpsPoint
from app.ingestion.eps_period_converter import build_standalone_eps_points


def _cum(
    quarter: int, cumulative_eps: float, available_at: dt.date, fiscal_year: int = 2026
):
    return ResolvedCumulativeEpsPoint(
        fiscal_year=fiscal_year,
        quarter=quarter,
        cumulative_eps=cumulative_eps,
        available_at=available_at,
    )


def test_q1_standalone_equals_q1_cumulative():
    points = [_cum(1, 2.0, dt.date(2026, 5, 10))]
    result = build_standalone_eps_points(cumulative_points=points)
    assert result == [
        QuarterlyEpsPoint(
            fiscal_year=2026, quarter=1, eps=2.0, available_at=dt.date(2026, 5, 10)
        )
    ]


def test_q2_standalone_derived_by_subtracting_q1():
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(2, 5.5, dt.date(2026, 8, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    q2 = next(p for p in result if p.quarter == 2)
    assert q2.eps == pytest.approx(3.5)  # 5.5 - 2.0


def test_q3_standalone_derived_by_subtracting_h1():
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(2, 5.5, dt.date(2026, 8, 10)),
        _cum(3, 8.0, dt.date(2026, 11, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    q3 = next(p for p in result if p.quarter == 3)
    assert q3.eps == pytest.approx(2.5)  # 8.0 - 5.5


def test_full_worked_example_q1_through_q3():
    """
    The complete worked example from this module's own design
    discussion: Q1=2.0, H1=5.5, 9M=8.0 cumulative -> standalone
    Q1=2.0, Q2=3.5, Q3=2.5.
    """
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(2, 5.5, dt.date(2026, 8, 10)),
        _cum(3, 8.0, dt.date(2026, 11, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    by_quarter = {p.quarter: p.eps for p in result}
    assert by_quarter == pytest.approx({1: 2.0, 2: 3.5, 3: 2.5})


def test_q2_available_at_is_max_of_q1_and_h1():
    # H1 published LATER than Q1 (the normal case).
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(2, 5.5, dt.date(2026, 8, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    q2 = next(p for p in result if p.quarter == 2)
    assert q2.available_at == dt.date(2026, 8, 10)


def test_q2_available_at_uses_later_q1_revision_if_it_postdates_h1():
    """
    Edge case worth pinning down explicitly: if Q1's cumulative figure
    is REVISED after H1 was already published (e.g. a restatement),
    the derived Q2 standalone point cannot be considered available any
    earlier than that later Q1 revision either — max() must consider
    both sides regardless of which one is normally expected to be
    older.
    """
    points = [
        _cum(1, 2.0, dt.date(2026, 9, 1)),  # a late Q1 revision
        _cum(2, 5.5, dt.date(2026, 8, 10)),  # H1 published earlier
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    q2 = next(p for p in result if p.quarter == 2)
    assert q2.available_at == dt.date(2026, 9, 1)


def test_q3_available_at_is_max_of_h1_and_9m():
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(2, 5.5, dt.date(2026, 8, 10)),
        _cum(3, 8.0, dt.date(2026, 11, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    q3 = next(p for p in result if p.quarter == 3)
    assert q3.available_at == dt.date(2026, 11, 10)


def test_q2_omitted_when_q1_missing():
    points = [_cum(2, 5.5, dt.date(2026, 8, 10))]
    result = build_standalone_eps_points(cumulative_points=points)
    assert result == []


def test_q3_omitted_when_h1_missing_even_if_q1_present():
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(3, 8.0, dt.date(2026, 11, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    assert [p.quarter for p in result] == [1]  # Q1 still resolves; Q3 does not


def test_q4_never_produced_even_when_present_in_input():
    """
    Q4 standalone (FY cumulative - 9M cumulative) is deliberately not
    supported yet — see module docstring. A Q4 cumulative point in the
    input must be silently excluded, not guessed at.
    """
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(2, 5.5, dt.date(2026, 8, 10)),
        _cum(3, 8.0, dt.date(2026, 11, 10)),
        _cum(4, 11.0, dt.date(2027, 3, 31)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    assert 4 not in {p.quarter for p in result}
    assert len(result) == 3  # Q1, Q2, Q3 only


def test_duplicate_quarter_in_input_is_dropped_not_guessed():
    """
    Two different cumulative figures claiming to be the SAME
    (fiscal_year, quarter) — e.g. an un-deduplicated revision history
    handed to this function by mistake — is ambiguous. This function
    does not guess which one is authoritative; resolving that
    ambiguity is the caller's job (a point-in-time observation store),
    not this converter's.
    """
    points = [
        _cum(1, 2.0, dt.date(2026, 5, 10)),
        _cum(1, 2.2, dt.date(2026, 6, 1)),  # conflicting duplicate for Q1
        _cum(2, 5.5, dt.date(2026, 8, 10)),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    # Q1 is ambiguous (dropped); Q2 depends on Q1, so it's dropped too.
    assert result == []


def test_diluted_cumulative_yoy_worked_example_demonstrates_why_conversion_matters():
    """
    The exact scenario that motivates this whole module: a strong Q1
    can keep an already-weakening Q2 CUMULATIVE YoY looking healthy
    purely by dilution, masking a real trend reversal that only shows
    up once each quarter is judged independently.

    This year:  Q1 cumulative = 130 (standalone +30% YoY)
                H1 cumulative = 216.55 (this makes standalone Q2 =
                86.55, which is -5% YoY vs last year's Q2 of 91.1)
    Last year:  Q1 cumulative = 100
                H1 cumulative = 191.1 (Q1=100, Q2=91.1)

    Naively comparing cumulative-to-cumulative YoY for H1
    (216.55 / 191.1 - 1 ≈ +13.3%) reads as solid, uninterrupted
    growth. But the TRUE standalone Q2 figure (86.55) is a -5% YoY
    DECLINE from last year's standalone Q2 (91.1) — a real reversal
    that the cumulative view obscures. This test only checks the
    conversion math; the point is illustrative documentation for why
    this module exists, not a build_eps_yoy call (that's covered in
    test_eps_growth_builder.py).
    """
    this_year = [
        _cum(1, 130.0, dt.date(2026, 5, 10), fiscal_year=2026),
        _cum(2, 216.55, dt.date(2026, 8, 10), fiscal_year=2026),
    ]
    last_year = [
        _cum(1, 100.0, dt.date(2025, 5, 10), fiscal_year=2025),
        _cum(2, 191.1, dt.date(2025, 8, 10), fiscal_year=2025),
    ]

    this_year_standalone = {
        p.quarter: p.eps
        for p in build_standalone_eps_points(cumulative_points=this_year)
    }
    last_year_standalone = {
        p.quarter: p.eps
        for p in build_standalone_eps_points(cumulative_points=last_year)
    }

    # Q1 standalone YoY: 130 / 100 - 1 = +30%
    q1_yoy = this_year_standalone[1] / last_year_standalone[1] - 1.0
    assert q1_yoy == pytest.approx(0.30)

    # Q2 standalone YoY: 86.55 / 91.1 - 1 ≈ -5% (a real decline)
    q2_yoy = this_year_standalone[2] / last_year_standalone[2] - 1.0
    assert q2_yoy == pytest.approx(-0.05, abs=1e-3)

    # Meanwhile the NAIVE cumulative-to-cumulative H1 YoY looks like
    # solid uninterrupted growth (+13.3%) — this is exactly the
    # misleading reading this module's conversion exists to avoid.
    naive_cumulative_h1_yoy = 216.55 / 191.1 - 1.0
    assert naive_cumulative_h1_yoy == pytest.approx(0.133, abs=1e-3)


def test_empty_input_returns_empty_list():
    assert build_standalone_eps_points(cumulative_points=[]) == []


def test_multiple_fiscal_years_are_converted_independently():
    points = [
        _cum(1, 2.0, dt.date(2025, 5, 10), fiscal_year=2025),
        _cum(2, 5.5, dt.date(2025, 8, 10), fiscal_year=2025),
        _cum(1, 2.2, dt.date(2026, 5, 10), fiscal_year=2026),
        _cum(2, 6.0, dt.date(2026, 8, 10), fiscal_year=2026),
    ]
    result = build_standalone_eps_points(cumulative_points=points)
    by_year_quarter = {(p.fiscal_year, p.quarter): p.eps for p in result}
    assert by_year_quarter[(2025, 2)] == pytest.approx(3.5)  # 5.5 - 2.0
    assert by_year_quarter[(2026, 2)] == pytest.approx(3.8)  # 6.0 - 2.2
