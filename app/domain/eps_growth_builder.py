"""
Pure, provider-independent quarterly-EPS YoY calculations.

DESIGN DECISION — why this module exists separately from
monthly_revenue_builder.py: EPS data in this project is sourced from
an OFFICIAL DISCLOSURE-DATE-ATTRIBUTED source (see this module's
ingestion counterpart, not yet built as of this module's introduction
— see StockFeatures.eps_growth_sustained and the project README's
Roadmap), NOT from FinMind's TaiwanStockFinancialStatements dataset.
That FinMind dataset's `date` field is the FISCAL PERIOD END DATE
(e.g. "2026-03-31" for Q1), not the date the figure was actually
disclosed to the market — Taiwan law gives listed companies up to 45
days after quarter-end (Q1-Q3) or until the following March (Q4/
annual) to publish. Treating the period-end date as if it were the
disclosure date would introduce a severe, systematic look-ahead bias
far worse than anything this project has accepted elsewhere. This
project's public disclosure source (MOPS-equivalent) is expected to
supply a REAL announcement date for every row, which is why
QuarterlyEpsPoint.available_at below is a required dt.date, not
Optional like MonthlyRevenuePoint.available_at — there is no
"legacy row predating a timestamp column" case here, unlike FinMind's
TaiwanStockMonthRevenue: every point from a disclosure-date-attributed
source is dated by construction.

DESIGN DECISION — EPS YoY undefined-comparison guard: unlike revenue,
EPS can legitimately be zero or negative (a quarterly loss). A naive
`current / previous - 1` computation on a sign change (e.g. previous
= -1, current = +1 giving -200%) produces a numerically well-formed
but economically meaningless percentage — it does not represent "200%
worse," it represents "swung from a loss to a profit." Rather than
inventing a special-cased business rule for sign flips, this module
follows the exact same philosophy as build_revenue_yoy(): a
non-positive denominator makes the comparison undefined, so it
returns None ("cannot be meaningfully expressed as YoY growth"), not
a numerically-computed but misleading figure.

DESIGN DECISION — build_eps_growth_sustained_signal()'s window is
QUARTERS, not months, and defaults to a 2-quarter window (both
quarters must clear the threshold), not revenue's 3-month/2-pass
pattern. Two reasons: (1) EPS is only reported 4 times a year, so a
3-quarter requirement would span 9+ months before the signal could
ever confirm sustained growth — too slow to be a useful "recent
trend" signal; (2) quarterly figures are inherently less noisy than
individual months (immune to shipment timing, working-day counts, and
other single-month distortions that motivated revenue's "allow one
soft month" leniency), so requiring both quarters in a 2-quarter
window to individually clear the bar is not an overly harsh
substitute for that leniency. The window/threshold/min-pass are still
independently parameterized (mirroring monthly_revenue_builder.py),
so a future strategy version can loosen this without a rewrite.

Same STRICTLY CONSECUTIVE requirement as monthly_revenue_builder.py's
revenue-growth-sustained signal: the window walks backward one
quarter at a time from the latest known-available quarter (e.g.
2026Q2 -> 2026Q1 -> 2025Q4), correctly rolling the fiscal year
backward at Q1. A gap quarter inside the window is never bridged by
an older quarter — it makes the whole signal None, exactly like a
missing calendar month does for revenue.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class QuarterlyEpsPoint:
    fiscal_year: int
    quarter: int
    eps: float
    available_at: dt.date

    def __post_init__(self) -> None:
        if self.quarter not in (1, 2, 3, 4):
            raise ValueError(f"quarter must be 1-4, got {self.quarter}")


EPS_GROWTH_THRESHOLD = 0.10
EPS_GROWTH_WINDOW_QUARTERS = 2
EPS_GROWTH_MIN_PASS_QUARTERS = 2


def _previous_quarter(fiscal_year: int, quarter: int) -> tuple[int, int]:
    if quarter == 1:
        return fiscal_year - 1, 4
    return fiscal_year, quarter - 1


def _build_trailing_quarters(
    *, latest_fiscal_year: int, latest_quarter: int, window_quarters: int
) -> list[tuple[int, int]]:
    """
    Return `window_quarters` STRICTLY CONSECUTIVE (fiscal_year, quarter)
    pairs, newest first, walking backward one quarter at a time from
    (latest_fiscal_year, latest_quarter) — e.g. latest=2026Q2,
    window=2 -> [(2026, 2), (2026, 1)]; latest=2026Q1, window=2 ->
    [(2026, 1), (2025, 4)]. This does NOT look at which quarters
    actually have data — that check happens per-quarter in
    _resolve_quarter_yoy — so a gap quarter produces a required
    quarter with no matching point, not a silently skipped one.
    """
    result: list[tuple[int, int]] = []
    fiscal_year, quarter = latest_fiscal_year, latest_quarter
    for _ in range(window_quarters):
        result.append((fiscal_year, quarter))
        fiscal_year, quarter = _previous_quarter(fiscal_year, quarter)
    return result


def _resolve_quarter_yoy(
    *,
    target_date: dt.date,
    fiscal_year: int,
    quarter: int,
    points: list[QuarterlyEpsPoint],
) -> float | None:
    """
    Resolve a single fiscal quarter's EPS YoY. Both the current-side
    and previous-year-same-quarter points must independently satisfy
    available_at <= target_date — there is no "undated legacy row"
    exemption here (see this module's docstring): every point from a
    disclosure-date-attributed source is dated, so a same-quarter
    revision published after target_date is excluded on EITHER side,
    from the outset, rather than as an after-the-fact fix (as it had
    to be for monthly_revenue_builder.build_revenue_yoy's denominator).
    """
    current_points = [
        point
        for point in points
        if point.fiscal_year == fiscal_year
        and point.quarter == quarter
        and point.available_at <= target_date
    ]
    if not current_points:
        return None

    current = max(current_points, key=lambda point: point.available_at)

    previous_year_points = [
        point
        for point in points
        if point.fiscal_year == fiscal_year - 1
        and point.quarter == quarter
        and point.available_at <= target_date
    ]
    if not previous_year_points:
        return None

    previous = max(previous_year_points, key=lambda point: point.available_at)

    if previous.eps <= 0:
        # Undefined comparison (zero/negative prior-year EPS) — see
        # module docstring. Not computed as a misleading percentage.
        return None

    return current.eps / previous.eps - 1.0


def build_eps_yoy(
    *,
    target_date: dt.date,
    points: list[QuarterlyEpsPoint],
) -> float | None:
    """
    Year-over-year EPS growth using the newest fiscal quarter known to
    be available (available_at <= target_date) as of target_date.

    Returns None if no eligible current-quarter point exists, if the
    matching previous-year same-quarter point is missing or itself
    not yet available as of target_date, or if the previous year's
    EPS is not positive (see module docstring for why this isn't
    computed as a sign-flip percentage).
    """
    eligible_points = [point for point in points if point.available_at <= target_date]
    if not eligible_points:
        return None

    latest = max(
        eligible_points,
        key=lambda point: (point.fiscal_year, point.quarter, point.available_at),
    )

    return _resolve_quarter_yoy(
        target_date=target_date,
        fiscal_year=latest.fiscal_year,
        quarter=latest.quarter,
        points=points,
    )


def build_eps_growth_sustained_signal(
    *,
    target_date: dt.date,
    points: list[QuarterlyEpsPoint],
    window_quarters: int = EPS_GROWTH_WINDOW_QUARTERS,
    threshold: float = EPS_GROWTH_THRESHOLD,
    min_pass_quarters: int = EPS_GROWTH_MIN_PASS_QUARTERS,
) -> bool | None:
    """
    Whether quarterly EPS YoY growth has been "sustained" over the
    `window_quarters` STRICTLY CONSECUTIVE fiscal quarters ending at
    the newest quarter known to be available as of target_date (see
    this module's docstring for the full rule and rationale).

    Returns None (data insufficient) when no eligible current-quarter
    point exists at all, or when ANY of the `window_quarters` required
    CONSECUTIVE quarters cannot be resolved (missing current-side
    point not yet available, missing/non-positive previous-year
    same-quarter EPS) — never computed from a partial or
    non-consecutive window, and never bridging a gap quarter with an
    older one.
    """
    if window_quarters <= 0:
        raise ValueError("window_quarters must be positive")
    if not (1 <= min_pass_quarters <= window_quarters):
        raise ValueError("min_pass_quarters must be between 1 and window_quarters")

    eligible_points = [point for point in points if point.available_at <= target_date]
    if not eligible_points:
        return None

    latest = max(
        eligible_points,
        key=lambda point: (point.fiscal_year, point.quarter, point.available_at),
    )

    required_quarters = _build_trailing_quarters(
        latest_fiscal_year=latest.fiscal_year,
        latest_quarter=latest.quarter,
        window_quarters=window_quarters,
    )

    yoy_values: list[float] = []
    for fiscal_year, quarter in required_quarters:
        yoy = _resolve_quarter_yoy(
            target_date=target_date,
            fiscal_year=fiscal_year,
            quarter=quarter,
            points=points,
        )
        if yoy is None:
            return None
        yoy_values.append(yoy)

    # required_quarters is newest-first, so yoy_values[0] is the
    # latest quarter's YoY.
    latest_yoy = yoy_values[0]
    pass_count = sum(1 for yoy in yoy_values if yoy >= threshold)

    return latest_yoy >= threshold and pass_count >= min_pass_quarters


def combine_fundamental_growth_signal(
    *,
    revenue: bool | None,
    eps: bool | None,
) -> bool | None:
    """
    Tri-state ("Kleene") OR: the report's "基本面" field is meant to
    answer the ORIGINAL spec's literal question — "營收或 EPS YoY >=
    10%，且具持續性" (revenue OR EPS) — as a single combined field,
    while revenue_growth_sustained and eps_growth_sustained remain
    separately inspectable (see StockFeatures / ReportStockView).

    Truth table:
    - Either side True  -> True  (the OR is already satisfied; the
      other side's value, even if still unknown, cannot change that)
    - Both sides False   -> False (both are conclusively known, and
      neither satisfies the condition)
    - Otherwise          -> None (at least one side is unknown and
      neither side is True — e.g. revenue=False, eps=None: EPS might
      still turn out to be True, so this must not be reported as a
      confident "False")

    This is standard three-valued logic OR, not an approximation of
    it — asymmetric with a plain `bool | None` "and both must be
    non-None" implementation, which would wrongly report None even
    when one side already independently proves True.
    """
    if revenue is True or eps is True:
        return True
    if revenue is False and eps is False:
        return False
    return None
