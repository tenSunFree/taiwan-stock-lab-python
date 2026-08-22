import datetime as dt
from decimal import Decimal

from app.ingestion.valuation_mapper import build_tpex_valuations, build_twse_valuations

TARGET_DATE = dt.date(2026, 8, 7)


# --- TWSE (verified field names) --------------------------------------------


def test_build_twse_valuations_maps_pe_ratio():
    rows = [
        {"Date": "1150807", "Code": "2330", "Name": "台積電", "PEratio": "23.45"},
    ]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert len(result) == 1
    assert result[0].stock_id == "2330"
    assert result[0].pe_ratio == Decimal("23.45")
    assert isinstance(result[0].pe_ratio, Decimal)


def test_build_twse_valuations_boundary_20_parses_exactly():
    rows = [{"Date": "1150807", "Code": "1101", "PEratio": "20"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result[0].pe_ratio == Decimal("20")


def test_build_twse_valuations_dash_means_no_pe_available():
    """TWSE emits "-" (not a number) when trailing EPS is zero/negative
    — this is the case app.domain.valuation_filter must fail-close on."""
    rows = [{"Date": "1150807", "Code": "9999", "PEratio": "-"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result[0].pe_ratio is None


def test_build_twse_valuations_empty_string_means_missing():
    rows = [{"Date": "1150807", "Code": "9999", "PEratio": ""}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result[0].pe_ratio is None


def test_build_twse_valuations_unparseable_string_means_missing():
    rows = [{"Date": "1150807", "Code": "9999", "PEratio": "not-a-number"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result[0].pe_ratio is None


def test_build_twse_valuations_does_not_coerce_negative_pe_to_none():
    """Mapper faithfully parses whatever the source sent, even if it
    happens to be a negative number — deciding that's invalid is
    valuation_filter's job, not the mapper's."""
    rows = [{"Date": "1150807", "Code": "9999", "PEratio": "-3.5"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result[0].pe_ratio == Decimal("-3.5")


def test_build_twse_valuations_skips_rows_missing_stock_id():
    rows = [{"Date": "1150807", "Code": "", "PEratio": "15"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result == []


def test_build_twse_valuations_uses_latest_date_at_or_before_target():
    """
    The real-world case that motivated this design: BWIBBU_ALL's Date
    commonly lags one calendar day behind target_date (confirmed via a
    live dry run) — this must be ACCEPTED (using yesterday's P/E), not
    rejected, unlike price-data staleness which genuinely does mean
    "not today's number."
    """
    rows = [{"Date": "1150806", "Code": "2330", "PEratio": "23.45"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert len(result) == 1
    assert result[0].stock_id == "2330"
    assert result[0].pe_ratio == Decimal("23.45")
    # trading_date reflects the actual snapshot date used (one day
    # before target_date here), not target_date itself.
    assert result[0].trading_date == dt.date(2026, 8, 6)


def test_build_twse_valuations_excludes_rows_dated_after_target():
    """The one direction that must still be rejected: a row dated
    AFTER target_date would be look-ahead bias, never acceptable."""
    rows = [{"Date": "1150808", "Code": "2330", "PEratio": "23.45"}]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert result == []


def test_build_twse_valuations_picks_newest_date_when_multiple_present():
    """When the snapshot contains more than one date (e.g. a slow
    rollover), only the single newest date <= target_date is used —
    older dates for the same stock are dropped, not averaged or kept
    alongside."""
    rows = [
        {"Date": "1150805", "Code": "2330", "PEratio": "20.00"},
        {"Date": "1150806", "Code": "2330", "PEratio": "23.45"},
    ]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert len(result) == 1
    assert result[0].pe_ratio == Decimal("23.45")
    assert result[0].trading_date == dt.date(2026, 8, 6)


def test_build_twse_valuations_handles_multiple_stocks():
    rows = [
        {"Date": "1150807", "Code": "2330", "PEratio": "23.45"},
        {"Date": "1150807", "Code": "1101", "PEratio": "12.10"},
        {"Date": "1150806", "Code": "9999", "PEratio": "99"},  # stale, skipped
    ]
    result = build_twse_valuations(target_date=TARGET_DATE, rows=rows)
    assert {r.stock_id for r in result} == {"2330", "1101"}


# --- TPEx (verified field names, confirmed against a real response) -------
#
# Real response is a bare JSON array (same shape as TWSE's, and as
# TPEx's own fetch_daily_price()) — build_tpex_valuations takes the
# row dicts directly, no unwrapping needed. (An earlier version of
# this comment claimed a {"value": [...], "Count": N} wrapper, based
# on a PowerShell Invoke-RestMethod/ConvertTo-Json round trip that
# turned out to have silently reshaped the response; see
# market_data_client.TpexClient.fetch_valuation's docstring.)


def test_build_tpex_valuations_maps_pe_ratio():
    rows = [
        {
            "Date": "1150807",
            "SecuritiesCompanyCode": "1240",
            "CompanyName": "測試公司",
            "PriceEarningRatio": "10.59",
            "DividendPerShare": "0.50000000",
            "YieldRatio": "0.88",
            "PriceBookRatio": "1.68",
        }
    ]
    result = build_tpex_valuations(target_date=TARGET_DATE, rows=rows)
    assert len(result) == 1
    assert result[0].stock_id == "1240"
    assert result[0].pe_ratio == Decimal("10.59")
    assert isinstance(result[0].pe_ratio, Decimal)


def test_build_tpex_valuations_na_means_no_pe_available():
    """TPEx emits the literal string "N/A" (confirmed from a real
    response) when trailing EPS is zero/negative — same meaning as
    TWSE's "-", must map to None so valuation_filter fail-closes."""
    rows = [
        {"Date": "1150807", "SecuritiesCompanyCode": "1569", "PriceEarningRatio": "N/A"}
    ]
    result = build_tpex_valuations(target_date=TARGET_DATE, rows=rows)
    assert result[0].pe_ratio is None


def test_build_tpex_valuations_skips_rows_missing_stock_id():
    rows = [
        {"Date": "1150807", "SecuritiesCompanyCode": "", "PriceEarningRatio": "18.27"}
    ]
    result = build_tpex_valuations(target_date=TARGET_DATE, rows=rows)
    assert result == []


def test_build_tpex_valuations_uses_latest_date_at_or_before_target():
    """Same real-world lag as TWSE's — see
    build_twse_valuations's equivalent test for the full reasoning."""
    rows = [
        {
            "Date": "1150806",
            "SecuritiesCompanyCode": "1240",
            "PriceEarningRatio": "10.59",
        }
    ]
    result = build_tpex_valuations(target_date=TARGET_DATE, rows=rows)
    assert len(result) == 1
    assert result[0].stock_id == "1240"
    assert result[0].pe_ratio == Decimal("10.59")
    assert result[0].trading_date == dt.date(2026, 8, 6)


def test_build_tpex_valuations_excludes_rows_dated_after_target():
    rows = [
        {
            "Date": "1150808",
            "SecuritiesCompanyCode": "1240",
            "PriceEarningRatio": "10.59",
        }
    ]
    result = build_tpex_valuations(target_date=TARGET_DATE, rows=rows)
    assert result == []


def test_build_tpex_valuations_handles_multiple_stocks():
    rows = [
        {
            "Date": "1150807",
            "SecuritiesCompanyCode": "1240",
            "PriceEarningRatio": "10.59",
        },
        {
            "Date": "1150807",
            "SecuritiesCompanyCode": "1259",
            "PriceEarningRatio": "18.36",
        },
        {
            "Date": "1150806",
            "SecuritiesCompanyCode": "9999",
            "PriceEarningRatio": "99",
        },  # stale
    ]
    result = build_tpex_valuations(target_date=TARGET_DATE, rows=rows)
    assert {r.stock_id for r in result} == {"1240", "1259"}
