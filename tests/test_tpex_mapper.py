import datetime as dt
from decimal import Decimal

from app.ingestion.tpex_mapper import build_daily_prices, roc_date_to_gregorian

TARGET_DATE = dt.date(2026, 8, 12)

SAMPLE_ROWS = [
    {
        "Date": "1150812",
        "SecuritiesCompanyCode": "006201",
        "CompanyName": "測試ETF",
        "Close": "45.21",
        "Change": "+1.41",
        "Open": "44.20",
        "High": "45.26",
        "Low": "44.20",
        "Average": "44.91",
        "TradingShares": "508551",
        "TransactionAmount": "22841328",
        "TransactionNumber": "930",
        "LatestBidPrice": "45.21",
        "LatesAskPrice": "45.23",
        "Capitals": "23446000",
        "NextReferencePrice": "45.21",
        "NextLimitUp": "49.73",
        "NextLimitDown": "40.69",
    },
    {
        "Date": "1150812",
        "SecuritiesCompanyCode": "6488",
        "CompanyName": "測試上櫃股",
        "Close": "0",
        "Change": "0.00",
        "Open": "0",
        "High": "0",
        "Low": "0",
        "Average": "0",
        "TradingShares": "0",
        "TransactionAmount": "0",
        "TransactionNumber": "0",
        "LatestBidPrice": "0",
        "LatesAskPrice": "0",
        "Capitals": "1000000",
        "NextReferencePrice": "0",
        "NextLimitUp": "0",
        "NextLimitDown": "0",
    },
]


def test_roc_date_conversion():
    assert roc_date_to_gregorian("1150812") == dt.date(2026, 8, 12)


def test_roc_date_conversion_rejects_malformed_input():
    assert roc_date_to_gregorian("") is None
    assert roc_date_to_gregorian("abc") is None


def test_reference_price_derived_from_close_minus_change():
    prices = build_daily_prices(target_date=TARGET_DATE, rows=SAMPLE_ROWS)
    etf = next(p for p in prices if p.stock_id == "006201")
    assert etf.close_price == Decimal("45.21")
    assert etf.reference_price == Decimal("43.80")  # 45.21 - 1.41


def test_negative_change_sign_parses_correctly():
    rows = [dict(SAMPLE_ROWS[0])]
    rows[0]["Change"] = "-1.41"
    prices = build_daily_prices(target_date=TARGET_DATE, rows=rows)
    assert prices[0].reference_price == Decimal("46.62")  # 45.21 - (-1.41)


def test_next_session_fields_are_not_mapped_into_daily_price():
    """NextReferencePrice/NextLimitUp/NextLimitDown must never leak
    into DailyPrice.limit_up_price or reference_price — those fields
    describe the NEXT session, not today."""
    prices = build_daily_prices(target_date=TARGET_DATE, rows=SAMPLE_ROWS)
    etf = next(p for p in prices if p.stock_id == "006201")
    assert etf.limit_up_price is None
    assert etf.reference_price != Decimal(
        "45.21"
    )  # not NextReferencePrice's value used as-is


def test_zero_price_row_treated_as_missing():
    prices = build_daily_prices(target_date=TARGET_DATE, rows=SAMPLE_ROWS)
    zero_row = next(p for p in prices if p.stock_id == "6488")
    assert zero_row.close_price is None
    assert zero_row.data_quality_ok is False


def test_rows_with_mismatched_date_are_skipped():
    stale_rows = [{**row, "Date": "1150811"} for row in SAMPLE_ROWS]
    prices = build_daily_prices(target_date=TARGET_DATE, rows=stale_rows)
    assert prices == []
