"""
Converts YEAR-TO-DATE CUMULATIVE EPS points (see app.ingestion.
eps_mapper's module docstring for why TWSE/TPEx's t187ap06_{L,O}_ci
figures are cumulative, not standalone-quarter) into genuine
STANDALONE-quarter app.domain.eps_growth_builder.QuarterlyEpsPoint
values, by subtracting the prior cumulative period:

    Q1 standalone = Q1 cumulative                (Q1 has no prior period)
    Q2 standalone = H1 cumulative  − Q1 cumulative
    Q3 standalone = 9M cumulative  − H1 cumulative

DESIGN DECISION — why this conversion exists at all: this project's
"EPS growth sustained" signal (build_eps_growth_sustained_signal) is
defined as "the latest quarter AND at least N of the trailing window
individually clear the growth threshold" — a claim about INDEPENDENT,
non-overlapping periods. Feeding it cumulative figures directly would
silently change its meaning to "have the year-to-date snapshots looked
good so far" — a materially weaker and different claim, since a
strong Q1 can keep an already-weakening Q2 cumulative YoY looking
healthy purely by dilution (see this module's own test suite for a
concrete worked example). Subtracting out the prior cumulative period
restores the original "each period judged on its own" semantics the
signal was designed around.

DESIGN DECISION — Q4 is NOT supported by this module yet. Q4
standalone would require `FY cumulative − 9M cumulative`, but whether
the annual-report endpoint's own EPS figure is really "full-year
cumulative" in the same sense as Q1-Q3 has NOT been independently
verified as of this module's introduction (annual reports in Taiwan
follow a different disclosure deadline and, in some datasets, a
different endpoint entirely from the Q1-Q3 quarterly filings) — see
this project's Roadmap for the pending verification step. Until that
is confirmed, any (fiscal_year, quarter=4) input is dropped
(excluded from the result), never guessed at.

DESIGN DECISION — available_at for a derived standalone point is
`max()` of the two cumulative points' own available_at, not either one
alone: deriving Q2 standalone requires BOTH the Q1 cumulative figure
AND the H1 cumulative figure to be known — the derived value cannot
be considered "available" any earlier than the LATER of the two
figures it depends on, even if one of them (typically Q1, published
months earlier) was known well in advance. Using anything less than
the max would silently reintroduce look-ahead bias into a derived
value despite each of its two inputs individually being safe.

DESIGN DECISION — revision propagation: because a standalone point is
DERIVED from two cumulative points, a later revision to EITHER input
(most often the earlier one, e.g. a restated Q1 cumulative EPS)
changes the derived standalone value for a DIFFERENT, LATER quarter
(Q2), not just the quarter that was itself revised. This module does
not need to know anything about revision history to get this right —
it operates purely on whatever ResolvedCumulativeEpsPoint values it is
given for a target_date, so as long as the caller supplies point-in-
time-correct cumulative points (see the project's planned revision-
safe observation store), this conversion is automatically revision-
safe too, with no special-casing required here.
"""

from __future__ import annotations

from app.domain.eps_growth_builder import QuarterlyEpsPoint
from app.ingestion.eps_availability_resolver import ResolvedCumulativeEpsPoint


def build_standalone_eps_points(
    *, cumulative_points: list[ResolvedCumulativeEpsPoint]
) -> list[QuarterlyEpsPoint]:
    """
    Convert resolved cumulative EPS points (for a SINGLE stock — see
    module docstring for the per-stock grouping convention this
    project uses throughout) into standalone-quarter QuarterlyEpsPoint
    values, one per (fiscal_year, quarter) pair for which conversion
    is currently possible.

    A (fiscal_year, quarter) pair is OMITTED from the result — never
    guessed at or computed from a partial input — when:
    - quarter == 4 (Q4 standalone is not yet supported; see module
      docstring)
    - quarter == 2 but the matching Q1 cumulative point for the same
      fiscal_year is missing (Q2 standalone needs both H1 and Q1)
    - quarter == 3 but the matching Q2 (H1) cumulative point for the
      same fiscal_year is missing (Q3 standalone needs both 9M and H1)
    - the same (fiscal_year, quarter) appears more than once in the
      input (ambiguous — which one is "the" cumulative figure for
      that period is the caller's job to resolve before calling this,
      e.g. via a point-in-time observation store; this function does
      not guess which duplicate to trust)

    Q1 is always its own standalone value (no subtraction needed,
    since Q1 cumulative and Q1 standalone are the same period), and is
    included whenever a Q1 cumulative point exists for that fiscal
    year, subject to the no-duplicates rule above.
    """
    by_year_quarter: dict[tuple[int, int], list[ResolvedCumulativeEpsPoint]] = {}
    for point in cumulative_points:
        by_year_quarter.setdefault((point.fiscal_year, point.quarter), []).append(point)

    # Fiscal years with an unambiguous (exactly one) point per quarter.
    unambiguous: dict[tuple[int, int], ResolvedCumulativeEpsPoint] = {
        key: points[0] for key, points in by_year_quarter.items() if len(points) == 1
    }

    results: list[QuarterlyEpsPoint] = []

    for (fiscal_year, quarter), point in unambiguous.items():
        if quarter == 1:
            results.append(
                QuarterlyEpsPoint(
                    fiscal_year=fiscal_year,
                    quarter=1,
                    eps=point.cumulative_eps,
                    available_at=point.available_at,
                )
            )
        elif quarter == 2:
            q1 = unambiguous.get((fiscal_year, 1))
            if q1 is None:
                continue
            results.append(
                QuarterlyEpsPoint(
                    fiscal_year=fiscal_year,
                    quarter=2,
                    eps=point.cumulative_eps - q1.cumulative_eps,
                    available_at=max(point.available_at, q1.available_at),
                )
            )
        elif quarter == 3:
            h1 = unambiguous.get((fiscal_year, 2))
            if h1 is None:
                continue
            results.append(
                QuarterlyEpsPoint(
                    fiscal_year=fiscal_year,
                    quarter=3,
                    eps=point.cumulative_eps - h1.cumulative_eps,
                    available_at=max(point.available_at, h1.available_at),
                )
            )
        # quarter == 4: not yet supported — see module docstring.

    return results
