import datetime as dt
from decimal import Decimal

from app.domain.models import Market, SecurityType
from app.ingestion.finmind_mapper import build_daily_prices, build_stock_master

TARGET_DATE = dt.date(2026, 8, 7)
PREVIOUS_DATE = dt.date(2026, 8, 6)


def test_build_stock_master_maps_market_from_type_field():
    rows = [
        {
            "stock_id": "2330",
            "stock_name": "台積電",
            "type": "twse",
            "industry_category": "半導體業",
        },
        {
            "stock_id": "6488",
            "stock_name": "環球晶",
            "type": "tpex",
            "industry_category": "半導體業",
        },
    ]
    result = build_stock_master(rows)
    assert result["2330"].market == Market.TWSE
    assert result["6488"].market == Market.TPEX
    assert result["2330"].stock_name == "台積電"


def test_build_stock_master_ignores_rows_outside_twse_tpex():
    rows = [
        {
            "stock_id": "1234",
            "stock_name": "興櫃股",
            "type": "emerging",
            "industry_category": "其他",
        }
    ]
    result = build_stock_master(rows)
    assert "1234" not in result


def test_etf_classified_from_industry_category():
    rows = [
        {
            "stock_id": "0050",
            "stock_name": "元大台灣50",
            "type": "twse",
            "industry_category": "ETF",
        }
    ]
    result = build_stock_master(rows)
    assert result["0050"].security_type == SecurityType.ETF


def test_four_digit_common_stock_classified_correctly():
    rows = [
        {
            "stock_id": "2330",
            "stock_name": "台積電",
            "type": "twse",
            "industry_category": "半導體業",
        }
    ]
    result = build_stock_master(rows)
    assert result["2330"].security_type == SecurityType.COMMON_STOCK


def test_unclassifiable_instrument_fails_closed_to_unknown():
    """Anything that doesn't match a known category AND isn't a plain
    4-digit numeric code must be UNKNOWN, never guessed as
    COMMON_STOCK — this is the fail-closed safety property
    CandidateBuilder depends on."""
    rows = [
        {
            "stock_id": "2330P1",
            "stock_name": "台積電特別股",
            "type": "twse",
            "industry_category": "特別股",
        }
    ]
    result = build_stock_master(rows)
    assert result["2330P1"].security_type == SecurityType.UNKNOWN


def test_four_digit_dr_classified_from_name_even_with_generic_category():
    """Regression test: a 4-digit TDR whose industry_category is
    generic (not explicitly 'DR'/'存託憑證') but whose stock_name
    contains the DR indicator must still be classified as DR, not
    fall through to the numeric-code COMMON_STOCK heuristic."""
    rows = [
        {
            "stock_id": "9105",
            "stock_name": "台灣存託憑證 ABC",
            "type": "twse",
            "industry_category": "電子業",  # generic category, no DR hint here
        }
    ]
    result = build_stock_master(rows)
    assert result["9105"].security_type == SecurityType.DR


def test_build_daily_prices_uses_previous_close_as_provisional_reference():
    today_rows = [
        {
            "date": "2026-08-07",
            "stock_id": "2330",
            "open": "600",
            "max": "610",
            "min": "598",
            "close": "605",
            "Trading_Volume": "10000000",
            "Trading_money": "6050000000",
        }
    ]
    previous_day_rows = [{"date": "2026-08-06", "stock_id": "2330", "close": "590"}]

    result = build_daily_prices(
        target_date=TARGET_DATE,
        today_rows=today_rows,
        previous_day_rows=previous_day_rows,
    )
    assert len(result) == 1
    assert result[0].reference_price == Decimal("590")
    assert result[0].close_price == Decimal("605")
    assert result[0].limit_up_price is None


def test_build_daily_prices_excludes_rows_with_wrong_date():
    today_rows = [
        {
            "date": "2026-08-06",  # wrong date — should be excluded
            "stock_id": "2330",
            "open": "600",
            "max": "610",
            "min": "598",
            "close": "605",
            "Trading_Volume": "10000000",
            "Trading_money": "6050000000",
        }
    ]
    result = build_daily_prices(
        target_date=TARGET_DATE, today_rows=today_rows, previous_day_rows=[]
    )
    assert result == []


def test_zero_price_is_treated_as_missing():
    """FinMind uses 0 for 'no announced price that day' — must not be
    treated as a real zero price."""
    today_rows = [
        {
            "date": "2026-08-07",
            "stock_id": "9999",
            "open": "0",
            "max": "0",
            "min": "0",
            "close": "0",
            "Trading_Volume": "0",
            "Trading_money": "0",
        }
    ]
    result = build_daily_prices(
        target_date=TARGET_DATE, today_rows=today_rows, previous_day_rows=[]
    )
    assert result[0].close_price is None
    assert result[0].data_quality_ok is False


def test_zero_volume_and_turnover_are_not_silently_treated_as_missing_but_fail_quality():
    """Volume/turnover of 0 is passed through as a real 0 (not
    None) — but data_quality_ok must still be False, since a real
    limit-up candidate cannot have zero traded volume."""
    today_rows = [
        {
            "date": "2026-08-07",
            "stock_id": "8888",
            "open": "100",
            "max": "110",
            "min": "100",
            "close": "110",
            "Trading_Volume": "0",
            "Trading_money": "0",
        }
    ]
    result = build_daily_prices(
        target_date=TARGET_DATE, today_rows=today_rows, previous_day_rows=[]
    )
    assert result[0].volume == 0
    assert result[0].turnover == Decimal("0")
    assert result[0].data_quality_ok is False


def test_invalid_numeric_string_does_not_raise():
    today_rows = [
        {
            "date": "2026-08-07",
            "stock_id": "7777",
            "open": "N/A",
            "max": "N/A",
            "min": "N/A",
            "close": "N/A",
            "Trading_Volume": "N/A",
            "Trading_money": "N/A",
        }
    ]
    result = build_daily_prices(
        target_date=TARGET_DATE, today_rows=today_rows, previous_day_rows=[]
    )
    assert result[0].close_price is None
    assert result[0].data_quality_ok is False


def test_missing_previous_day_leaves_reference_price_none():
    today_rows = [
        {
            "date": "2026-08-07",
            "stock_id": "2330",
            "open": "600",
            "max": "610",
            "min": "598",
            "close": "605",
            "Trading_Volume": "10000000",
            "Trading_money": "6050000000",
        }
    ]
    result = build_daily_prices(
        target_date=TARGET_DATE, today_rows=today_rows, previous_day_rows=[]
    )
    assert result[0].reference_price is None
