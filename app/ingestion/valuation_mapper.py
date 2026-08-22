"""
Map TWSE BWIBBU_ALL / TPEx tpex_mainboard_peratio_analysis raw JSON
into StockValuation domain models.

Both TWSE's and TPEx's field names are verified against real
responses (confirmed via a raw HTTP body dump, not a PowerShell
Invoke-RestMethod/ConvertTo-Json round trip — see
market_data_client.TpexClient.fetch_valuation's docstring for why
that distinction matters: PowerShell's JSON cmdlets can silently
reshape a bare array into something that LOOKS like an
{"value": [...], "Count": N} envelope even when the server never sent
one):
  TWSE (market_data_client.TwseClient.fetch_valuation): Date / Code /
      Name / PEratio / DividendYield / PBratio. Bare JSON array.
  TPEx (market_data_client.TpexClient.fetch_valuation): Date /
      SecuritiesCompanyCode / CompanyName / PriceEarningRatio /
      DividendPerShare / YieldRatio / PriceBookRatio. ALSO a bare
      JSON array, same shape as TWSE's — not wrapped in any envelope.

DATE HANDLING — observed via a real dry run (2026-08-21): TWSE's
BWIBBU_ALL Date lagged one calendar day behind fetch_daily_price()'s
STOCK_DAY_ALL for that single run. This is treated as a POLICY
decision, not a confirmed permanent invariant from one observation:
P/E depends on data (EPS, dividends) that is plausibly not finalized
as fast as a closing price, so accepting the newest available date
<= target_date (rather than requiring exact equality) is the more
defensible default regardless of the exact lag on any given day.
Both build_*_valuations functions below therefore use "the newest
available date <= target_date" — the same look-ahead-safe pattern as
app.domain.monthly_revenue_builder.build_revenue_yoy — rather than
requiring exact equality to target_date the way twse_mapper /
tpex_mapper's daily PRICE parsers correctly do (for price data,
staleness genuinely does mean "not today's number").

A staleness ceiling (see app.jobs.daily_ranking.
MAXIMUM_VALUATION_STALENESS_DAYS) still rejects a snapshot whose
newest available date is implausibly far behind target_date — "accept
a short lag" is not the same policy as "accept arbitrarily old data,"
and this module does not by itself enforce that ceiling; the caller
does, right after these functions return.

Deliberately does NOT coerce a non-positive or otherwise "invalid" P/E
into None here. The mapper's only job is faithfully parsing what the
source actually sent (blank/dash/N/A -> None, because that's genuinely
"no value present"; a parseable negative number -> that Decimal,
unchanged). Whether 0 < P/E <= threshold is a business rule that
belongs in app.domain.valuation_filter, not in this parsing step.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain.models import StockValuation
from app.ingestion.twse_mapper import roc_date_to_gregorian

_MISSING_VALUE_TOKENS = {"", "-", "--", "N/A", "NA", "null", "None"}


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None

    text = str(value).strip().replace(",", "")
    if text in _MISSING_VALUE_TOKENS:
        return None

    try:
        result = Decimal(text)
    except (InvalidOperation, ValueError):
        return None

    if not result.is_finite():
        return None

    return result


def build_twse_valuations(
    *, target_date: dt.date, rows: list[dict[str, Any]]
) -> list[StockValuation]:
    """
    Uses the newest available snapshot date that is <= target_date —
    NOT strict equality to target_date. Confirmed via a real dry run:
    BWIBBU_ALL's Date lags one calendar day behind
    fetch_daily_price()'s STOCK_DAY_ALL (P/E depends on data — EPS,
    dividends — that isn't finalized as fast as a closing price is).
    Requiring exact equality here would make the P/E filter never
    produce usable data at all, which is a strictly worse failure mode
    than "using yesterday's P/E" (unlike price data, where staleness
    genuinely means "not today's number" and IS the correct thing to
    reject). Same look-ahead-safe "latest available as of target_date"
    pattern as app.domain.monthly_revenue_builder.build_revenue_yoy —
    rows dated AFTER target_date are still excluded (never look ahead).

    The resulting StockValuation.trading_date reflects the actual
    snapshot date used, which may be earlier than target_date — this
    is the honest date the P/E figures are from, not necessarily
    "today."
    """
    dated_rows: list[tuple[dt.date, str, dict[str, Any]]] = []

    for row in rows:
        row_date = roc_date_to_gregorian(str(row.get("Date", "")))
        if row_date is None or row_date > target_date:
            continue

        stock_id = str(row.get("Code") or "").strip()
        if not stock_id:
            continue

        dated_rows.append((row_date, stock_id, row))

    if not dated_rows:
        return []

    latest_date = max(row_date for row_date, _, _ in dated_rows)

    return [
        StockValuation(
            trading_date=latest_date,
            stock_id=stock_id,
            pe_ratio=_to_decimal(row.get("PEratio")),
        )
        for row_date, stock_id, row in dated_rows
        if row_date == latest_date
    ]


def build_tpex_valuations(
    *, target_date: dt.date, rows: list[dict[str, Any]]
) -> list[StockValuation]:
    """
    Same "latest available as of target_date" pattern as
    build_twse_valuations() — see that function's docstring for why
    strict equality to target_date is wrong for valuation data (unlike
    price data). rows is TPEx's raw bare JSON array (see
    market_data_client.TpexClient.fetch_valuation's docstring), the
    same shape app.jobs.daily_ranking passes in for TWSE's
    build_twse_valuations — no unwrapping needed for either.
    """
    dated_rows: list[tuple[dt.date, str, dict[str, Any]]] = []

    for row in rows:
        row_date = roc_date_to_gregorian(str(row.get("Date", "")))
        if row_date is None or row_date > target_date:
            continue

        stock_id = str(row.get("SecuritiesCompanyCode") or "").strip()
        if not stock_id:
            continue

        dated_rows.append((row_date, stock_id, row))

    if not dated_rows:
        return []

    latest_date = max(row_date for row_date, _, _ in dated_rows)

    return [
        StockValuation(
            trading_date=latest_date,
            stock_id=stock_id,
            pe_ratio=_to_decimal(row.get("PriceEarningRatio")),
        )
        for row_date, stock_id, row in dated_rows
        if row_date == latest_date
    ]
