import datetime as dt
from decimal import Decimal

from app.ingestion.twse_mapper import (
    build_daily_prices,
    parse_stock_day_all_csv,
    roc_date_to_gregorian,
)

SAMPLE_CSV = (
    "日期,證券代號,證券名稱,成交股數,成交金額,開盤價,最高價,最低價,收盤價,漲跌價差,成交筆數\n"
    '"1150807","2330","台積電","24414025","57947015347","2390.00","2395.00","2355.00","2370.00","5.0000","64670"\n'
    '"1150807","1101","台泥","18367476","450087960","24.30","24.75","24.30","24.35","0.0000","6056"\n'
    '"1150807","8101","華冠","154","1924","0.00","0.00","0.00","0.00","0.0000","2"\n'
)

TARGET_DATE = dt.date(2026, 8, 7)


def test_roc_date_conversion():
    assert roc_date_to_gregorian("1150807") == dt.date(2026, 8, 7)


def test_roc_date_conversion_rejects_malformed_input():
    assert roc_date_to_gregorian("") is None
    assert roc_date_to_gregorian("abc") is None
    assert roc_date_to_gregorian("115-08-07") is None


def test_parse_csv_returns_row_dicts():
    rows = parse_stock_day_all_csv(SAMPLE_CSV)
    assert len(rows) == 3
    assert rows[0]["證券代號"] == "2330"


def test_reference_price_derived_from_price_change():
    rows = parse_stock_day_all_csv(SAMPLE_CSV)
    prices = build_daily_prices(target_date=TARGET_DATE, rows=rows)
    tsmc = next(p for p in prices if p.stock_id == "2330")
    assert tsmc.close_price == Decimal("2370.00")
    assert tsmc.reference_price == Decimal("2365.00")  # 2370.00 - 5.0000


def test_zero_price_change_still_derives_reference_price():
    rows = parse_stock_day_all_csv(SAMPLE_CSV)
    prices = build_daily_prices(target_date=TARGET_DATE, rows=rows)
    taicement = next(p for p in prices if p.stock_id == "1101")
    assert taicement.reference_price == Decimal("24.35")  # unchanged from close


def test_no_trade_row_treats_zero_price_as_missing():
    rows = parse_stock_day_all_csv(SAMPLE_CSV)
    prices = build_daily_prices(target_date=TARGET_DATE, rows=rows)
    huaguan = next(p for p in prices if p.stock_id == "8101")
    assert huaguan.close_price is None
    assert huaguan.data_quality_ok is False
    # volume/turnover pass through as real values, not treated as missing
    assert huaguan.volume == 154
    assert huaguan.turnover == Decimal("1924")


def test_rows_with_mismatched_date_are_skipped():
    csv_text = SAMPLE_CSV.replace("1150807", "1150806")
    rows = parse_stock_day_all_csv(csv_text)
    prices = build_daily_prices(target_date=TARGET_DATE, rows=rows)
    assert prices == []
