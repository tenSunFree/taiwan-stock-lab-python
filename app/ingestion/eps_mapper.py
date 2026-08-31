"""
Ingestion-layer parsing for TWSE/TPEx quarterly-financial-statement
OpenAPI responses (verified against real responses from
https://openapi.twse.com.tw/v1/opendata/t187ap06_L_ci — 上市公司綜合
損益表-一般業 — and http://mopsfin.twse.com.tw/opendata/t187ap06_O_ci
— 上櫃公司綜合損益表-一般業. Field names and formats are IDENTICAL
between the two — only the `_L_`/`_O_` segment of the endpoint path
differs — so this single mapper serves both markets' general-industry
filings. NOT yet verified for other industry types on either market
(financial holding / bank / securities / insurance report EPS via
different endpoints entirely — t187ap06_X_fh / _basi / _bd / _ins —
which this mapper does not target).

DESIGN DECISION — RawCumulativeEps, not "RawQuarterlyEps": this is a
deliberate rename from an earlier draft of this module. Verified
directly against real data (cross-checked TWSE's `基本每股盈餘（元）`
and `營業收入` for 台泥/1101 against its own separately-reported
monthly revenue figures): the values in a `季別=2` (Q2) row are
YEAR-TO-DATE CUMULATIVE (Jan-Jun), not standalone-quarter (Apr-Jun)
figures — a `季別=3` row is similarly Jan-Sep cumulative. Only
`季別=1` (Q1) rows are "cumulative" and "standalone" for the same
value, since Q1 is the year's first period. This matches Taiwan's
standard financial-statement convention (綜合損益表 is presented on a
year-to-date basis), which this project's earlier "RawQuarterlyEps"
name obscured and could have led a future reader to wrongly treat
these as independent, non-overlapping quarterly observations.

Converting these into genuine STANDALONE per-quarter figures (Q2
standalone = H1 cumulative − Q1 cumulative, etc.) is a LATER layer's
job — see app.ingestion.eps_period_converter — not this module's.
This module's only job is turning raw JSON/CSV rows into a clean,
strongly-typed RawCumulativeEps per company/quarter, dropping
anything malformed rather than guessing at it. Likewise, resolving
`出表日期` ("batch report date" — the date the WHOLE dataset was
compiled and published, IDENTICAL ACROSS EVERY COMPANY in the same
fetch, confirmed by direct inspection of a real multi-company
response) into a sharper, still-safe available_at is
app.ingestion.eps_availability_resolver's job, not this module's.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class RawCumulativeEps:
    stock_id: str
    fiscal_year: int
    quarter: int
    cumulative_eps: (
        float  # year-to-date cumulative, NOT standalone — see module docstring
    )
    batch_report_date: dt.date  # NOT a disclosure date — see module docstring


def _parse_roc_date(roc_date_str: str) -> dt.date | None:
    """
    Parse a ROC-calendar date string as used in TWSE's `出表日期`
    field (e.g. "1150831" -> 2026-08-31: the last 4 digits are MMDD,
    everything before that is the ROC year). Returns None — never
    raises — on any malformed input, so the caller can drop a single
    corrupt row instead of aborting the whole batch.

    NOTE: if this project's app.ingestion.twse_mapper (or tpex_mapper)
    already exposes a shared ROC-date parser used for other TWSE/TPEx
    date fields, prefer importing and reusing that instead of this
    local copy — two independently-maintained ROC-date parsers is a
    real (if subtle) risk of them silently drifting apart over time.
    This local copy exists because that shared utility's exact name
    and signature were not available to verify at the time this
    module was written.
    """
    if not roc_date_str or len(roc_date_str) < 5:
        return None
    try:
        roc_year = int(roc_date_str[:-4])
        month = int(roc_date_str[-4:-2])
        day = int(roc_date_str[-2:])
        return dt.date(roc_year + 1911, month, day)
    except (ValueError, TypeError):
        return None


def build_raw_cumulative_eps_points(
    *, rows: list[dict[str, str]]
) -> list[RawCumulativeEps]:
    """
    Parse TWSE/TPEx t187ap06_{L,O}_ci-shaped rows into
    RawCumulativeEps. Verified against real responses from BOTH the
    TWSE (`_L_`) and TPEx (`_O_`) general-industry endpoints — field
    names and row shape are identical between the two.

    A row is DROPPED (excluded from the result, never guessed at or
    substituted), rather than raising, when:
    - `公司代號` (stock_id) is missing or empty
    - `年度` (fiscal year, ROC) or `季別` (quarter) is missing or
      can't be parsed as an integer, or the parsed quarter isn't 1-4
    - `基本每股盈餘（元）` (cumulative EPS) is missing, empty, or
      unparseable — this legitimately happens for financial-industry
      / holding-company / securities-industry rows, which report EPS
      via a DIFFERENT TWSE/TPEx endpoint (t187ap06_X_fh / _basi / _bd
      / _ins) with different column layouts, not this general-
      industry one
    - `出表日期` (batch report date) is missing or can't be parsed as
      a ROC date

    This mirrors this project's existing fail-closed-per-row
    convention elsewhere (e.g. finmind_mapper.build_monthly_revenue_
    points: a malformed row is silently excluded, never guessed at).
    """
    points: list[RawCumulativeEps] = []

    for row in rows:
        stock_id = (row.get("公司代號") or "").strip()
        if not stock_id:
            continue

        fiscal_year_raw = row.get("年度")
        quarter_raw = row.get("季別")
        if not fiscal_year_raw or not quarter_raw:
            continue

        try:
            fiscal_year = int(fiscal_year_raw) + 1911
            quarter = int(quarter_raw)
        except (ValueError, TypeError):
            continue

        if quarter not in (1, 2, 3, 4):
            continue

        cumulative_eps_raw = row.get("基本每股盈餘（元）")
        if cumulative_eps_raw is None or cumulative_eps_raw == "":
            continue
        try:
            cumulative_eps = float(cumulative_eps_raw)
        except (ValueError, TypeError):
            continue

        batch_report_date = _parse_roc_date(row.get("出表日期") or "")
        if batch_report_date is None:
            continue

        points.append(
            RawCumulativeEps(
                stock_id=stock_id,
                fiscal_year=fiscal_year,
                quarter=quarter,
                cumulative_eps=cumulative_eps,
                batch_report_date=batch_report_date,
            )
        )

    return points
