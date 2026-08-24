"""
Shared domain models.

Deliberately decoupled from what any particular data source looks
like: regardless of the raw fields returned by FinMind / TWSE / TPEx,
everything must be converted into the clean models defined here before
being passed down to limit-up detection, candidate building, scoring,
etc.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Market(StrEnum):
    TWSE = "TWSE"
    TPEX = "TPEX"


class SecurityType(StrEnum):
    COMMON_STOCK = "COMMON_STOCK"
    ETF = "ETF"
    ETN = "ETN"
    WARRANT = "WARRANT"
    DR = "DR"  # depositary receipt
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StockMaster:
    stock_id: str
    stock_name: str
    market: Market
    security_type: SecurityType
    industry: str | None = None
    is_active: bool = True
    is_attention: bool | None = None  # under "attention" watch status; None = unknown
    # under disposition/restricted trading; None = unknown
    is_disposition: bool | None = None
    is_managed: bool | None = None  # full-cash-delivery / managed stock; None = unknown


@dataclass(frozen=True)
class DailyPrice:
    """
    Cleaned daily price record.

    reference_price / limit_up_price may be None — never assume any
    data source provides these fields. None means "this source did not
    provide it; try another source or fall back to calculation," and
    that decision is deferred to the caller (the limit_up module), not
    made here.
    """

    trading_date: date
    stock_id: str
    reference_price: Decimal | None
    open_price: Decimal | None
    high_price: Decimal | None
    low_price: Decimal | None
    close_price: Decimal | None
    volume: int | None
    turnover: Decimal | None
    limit_up_price: Decimal | None = None  # source-provided limit-up price, if any
    has_price_limit_today: bool = True  # False = special day with no daily price limit
    data_quality_ok: bool = True


@dataclass(frozen=True)
class StockValuation:
    """
    Daily P/E ratio snapshot, sourced from TWSE's BWIBBU_ALL /
    TPEx's tpex_mainboard_peratio_analysis — whole-market snapshots,
    same "latest trading day only" limitation as DailyPrice's own
    TWSE/TPEx sources.

    pe_ratio may be None: TWSE/TPEx do not compute a P/E ratio when a
    company's trailing EPS is zero or negative — that is a genuinely
    missing value, not a data error, and callers (see
    app.domain.valuation_filter) must treat it as "cannot verify the
    P/E threshold," not as "assume it passes."

    Deliberately just pe_ratio for now (YAGNI) — dividend_yield and
    pb_ratio aren't used by any current strategy rule; add them only
    when an actual rule needs them.
    """

    trading_date: date
    stock_id: str
    pe_ratio: Decimal | None


@dataclass(frozen=True)
class RegulatoryRiskStatus:
    """
    Official 注意/處置 (attention/disposition) status for one stock, on
    one trading date — sourced from TWSE's and TPEx's own announcement
    pages (see app.ingestion.regulatory_mapper's module docstring for
    the verified endpoints).

    MERGE TARGET, not a single source's output: attention and
    disposition are two separate official reports (separate query
    pages, separate endpoints, TWSE vs TPEx) — a mapper only ever
    knows about the one report it parsed. app.jobs.daily_ranking is
    responsible for combining an attention-source hit and a
    disposition-source hit for the SAME stock_id into one
    RegulatoryRiskStatus (OR-merge per field, not last-write-wins —
    a stock can genuinely be both at once). "managed"/full-cash-
    delivery (全額交割) status has no verified TWSE/TPEx source yet as
    of this rollout (same "known data gap" status as
    consecutive_limit_up_days — see README) and deliberately has no
    field here; app.domain.risk_policy.RiskPolicy.assess() still
    accepts is_managed as a parameter, this dataclass just never
    supplies a non-None value for it yet.

    A RegulatoryRiskStatus is only ever constructed for a stock that
    actually appeared in a source's "currently flagged" list — there
    is no "confirmed clean, explicitly checked" instance of this
    class. Confirming a stock is NOT currently flagged is a lookup
    miss (absent from the dict app.jobs.daily_ranking builds from the
    mapper output), not a RegulatoryRiskStatus with every field False.
    That lookup-miss-means-False mapping is only valid once the WHOLE
    source's fetch for this trading date is confirmed to have
    succeeded — see daily_ranking's WAITING_FOR_DATA handling, which
    covers the "cannot verify at all" case for this data the same way
    it does for TWSE/TPEx price and valuation snapshots. This
    dataclass's own default values are for the merge step's
    convenience only (e.g. "found in attention's list, so
    is_disposition just defaults False on this instance until the
    disposition mapper's hit — if any — gets merged in"), not a
    general-purpose three-state signal on their own.
    """

    trading_date: date
    stock_id: str

    is_attention: bool = False
    attention_reason: str | None = None

    is_disposition: bool = False
    disposition_start_date: date | None = None
    disposition_end_date: date | None = None
    disposition_reason: str | None = None
    disposition_measure: str | None = None
