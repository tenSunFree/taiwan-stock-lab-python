import datetime as dt
from decimal import Decimal

from app.domain.models import DailyPrice
from app.domain.risk_inputs import is_ky_stock, is_one_price_limit_up


def test_is_ky_stock_matches_suffix():
    assert is_ky_stock("測試-KY") is True
    assert is_ky_stock("F-KY控股") is True


def test_is_ky_stock_no_suffix():
    assert is_ky_stock("台積電") is False


def _make_price(**overrides):
    defaults = dict(
        trading_date=dt.date(2026, 8, 14),
        stock_id="1101",
        reference_price=Decimal("100"),
        open_price=Decimal("110"),
        high_price=Decimal("110"),
        low_price=Decimal("110"),
        close_price=Decimal("110"),
        volume=1000,
        turnover=Decimal("100000"),
    )
    defaults.update(overrides)
    return DailyPrice(**defaults)


def test_is_one_price_limit_up_true_when_ohlc_all_equal():
    price = _make_price()
    assert is_one_price_limit_up(price=price, limit_up_price=Decimal("110")) is True


def test_is_one_price_limit_up_false_when_open_differs():
    price = _make_price(open_price=Decimal("105"))
    assert is_one_price_limit_up(price=price, limit_up_price=Decimal("110")) is False


def test_is_one_price_limit_up_false_when_limit_up_price_none():
    price = _make_price()
    assert is_one_price_limit_up(price=price, limit_up_price=None) is False


def test_is_one_price_limit_up_false_when_any_ohlc_field_missing():
    price = _make_price(low_price=None)
    assert is_one_price_limit_up(price=price, limit_up_price=Decimal("110")) is False
