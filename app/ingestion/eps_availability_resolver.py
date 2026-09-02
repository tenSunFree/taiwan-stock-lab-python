"""
Resolves a genuine, look-ahead-safe `available_at` for a CUMULATIVE
EPS figure (see app.ingestion.eps_mapper's module docstring for why
these are year-to-date cumulative, not standalone-quarter, values)
from whichever signal this project currently has, in precedence
order:

    1. first_seen_at — the date this project's OWN daily ingestion job
       first observed this exact (stock_id, fiscal_year, quarter,
       cumulative_eps) VALUE in the TWSE/TPEx OpenAPI response. This
       is the most precise available_at this project can currently
       produce. To be precise about what it actually represents: it
       is NOT the company's true disclosure date — it is the date
       THIS PROJECT'S OWN PIPELINE first observed this exact figure,
       which is always >= the true disclosure date (the company must
       have disclosed before any downstream data source, including
       this pipeline, could see it), never earlier. That ordering is
       what makes it look-ahead-safe, not any claim of precision
       about the real-world disclosure event itself.
    2. batch_report_date — TWSE/TPEx's own `出表日期` (see
       app.ingestion.eps_mapper's module docstring), the date the
       WHOLE dataset was compiled, identical across every company in
       the same fetch. Safe (always >= the true per-company disclosure
       date) but coarse — used only when first_seen_at isn't available
       yet (e.g. historical quarters from before this project started
       its own daily observation).

DESIGN DECISION — no synthetic third fallback (e.g. "quarter-end +
statutory 45-day filing deadline"): this project's guiding principle,
established for monthly revenue, is that uncertain data resolves to
None or to a real, OBSERVED date — never an invented one. Both
first_seen_at and batch_report_date are real, observed dates; a
statutory-deadline estimate would not be, so it is deliberately
excluded here, the same way monthly_revenue_builder never reconstructs
a missing available_at from a calendar heuristic.

DESIGN DECISION — this module resolves availability for CUMULATIVE
points and stops there; it deliberately does NOT also perform the
cumulative -> standalone-quarter conversion (see
app.ingestion.eps_period_converter for that). Those are two
independent concerns: "when did we learn this cumulative figure" has
nothing to do with "how do we turn a cumulative figure into a
standalone-quarter one," and the converter needs the RESOLVED
available_at of potentially two different cumulative points (e.g. Q1
and H1) to compute the derived standalone point's own available_at —
see that module's docstring for why it must be a max() of both.

A future higher-fidelity disclosure-date source (e.g. an official,
program-accessible per-company filing-date feed, if one is ever
found — MOPS's own per-company query system was investigated and
rejected for automated ingestion; see this module's originating
design discussion) can be slotted in as a NEW highest-priority case
ahead of first_seen_at without touching anything downstream of this
module — every caller already only ever sees the resolved
available_at, never which source produced it, except via
ResolvedEpsAvailability.source for observability/debugging.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum

from app.ingestion.eps_mapper import RawCumulativeEps


class EpsAvailabilitySource(str, Enum):
    FIRST_SEEN = "first_seen"
    BATCH_REPORT_DATE = "batch_report_date"


@dataclass(frozen=True)
class ResolvedEpsAvailability:
    available_at: dt.date
    source: EpsAvailabilitySource


@dataclass(frozen=True)
class ResolvedCumulativeEpsPoint:
    """
    A RawCumulativeEps with its available_at fully resolved. This is
    deliberately NOT app.domain.eps_growth_builder.QuarterlyEpsPoint —
    that type represents a STANDALONE-quarter EPS value (see
    app.ingestion.eps_period_converter), while this one still holds
    the raw year-to-date cumulative figure. Conflating the two would
    silently reintroduce the exact cumulative-vs-standalone confusion
    this module's sibling modules exist to avoid.
    """

    fiscal_year: int
    quarter: int
    cumulative_eps: float
    available_at: dt.date


def resolve_eps_availability(
    *,
    first_seen_at: dt.date | None,
    batch_report_date: dt.date,
) -> ResolvedEpsAvailability:
    """
    batch_report_date is required (not Optional), not defaulted: every
    RawCumulativeEps carries one by construction (see
    app.ingestion.eps_mapper.build_raw_cumulative_eps_points, which
    drops any row where it can't be parsed) — there is no "neither
    source available" case for this function to handle.
    """
    if first_seen_at is not None:
        return ResolvedEpsAvailability(
            available_at=first_seen_at,
            source=EpsAvailabilitySource.FIRST_SEEN,
        )
    return ResolvedEpsAvailability(
        available_at=batch_report_date,
        source=EpsAvailabilitySource.BATCH_REPORT_DATE,
    )


def build_resolved_cumulative_eps_point(
    *,
    raw: RawCumulativeEps,
    first_seen_at: dt.date | None,
) -> ResolvedCumulativeEpsPoint:
    """
    Combine a RawCumulativeEps (parsed straight from the TWSE/TPEx
    OpenAPI response) with this project's own first-seen observation
    (if one exists yet) into a ResolvedCumulativeEpsPoint with a
    resolved, look-ahead-safe available_at. See this module's
    docstring for the full precedence rule and rationale.

    stock_id is intentionally dropped here, matching this project's
    existing convention for MonthlyRevenuePoint/QuarterlyEpsPoint: the
    caller is responsible for keeping points grouped per stock_id
    before this function is called per-stock.
    """
    resolved = resolve_eps_availability(
        first_seen_at=first_seen_at,
        batch_report_date=raw.batch_report_date,
    )
    return ResolvedCumulativeEpsPoint(
        fiscal_year=raw.fiscal_year,
        quarter=raw.quarter,
        cumulative_eps=raw.cumulative_eps,
        available_at=resolved.available_at,
    )
