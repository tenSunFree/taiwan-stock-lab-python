import datetime as dt
from decimal import Decimal

from app.domain.candidate_builder import CandidateBuilder
from app.domain.models import DailyPrice, Market, SecurityType, StockMaster

TRADING_DATE = dt.date(2026, 8, 7)


def make_stock(stock_id, security_type=SecurityType.COMMON_STOCK, is_active=True):
    return StockMaster(
        stock_id=stock_id,
        stock_name=f"Stock{stock_id}",
        market=Market.TWSE,
        security_type=security_type,
        is_active=is_active,
    )


def make_price(stock_id, close, ref, turnover, volume=1000, limit_up=None):
    return DailyPrice(
        trading_date=TRADING_DATE,
        stock_id=stock_id,
        reference_price=ref,
        open_price=ref,
        high_price=close,
        low_price=ref,
        close_price=close,
        volume=volume,
        turnover=turnover,
        limit_up_price=limit_up,
    )


def test_builder_keeps_only_common_stock_limit_up():
    stocks = [
        make_stock("1101"),
        make_stock("0050", security_type=SecurityType.ETF),
    ]
    prices = [
        make_price("1101", close=Decimal("44.65"), ref=Decimal("40.60"), turnover=Decimal("100000000")),
        make_price("0050", close=Decimal("44.65"), ref=Decimal("40.60"), turnover=Decimal("999999999")),
    ]

    builder = CandidateBuilder(minimum_turnover=Decimal("50000000"))
    candidates = builder.build(stocks, prices)

    assert [c.stock.stock_id for c in candidates] == ["1101"]


def test_builder_excludes_below_minimum_turnover():
    stocks = [make_stock("1101")]
    prices = [
        make_price("1101", close=Decimal("44.65"), ref=Decimal("40.60"), turnover=Decimal("100")),
    ]
    builder = CandidateBuilder(minimum_turnover=Decimal("50000000"))
    assert builder.build(stocks, prices) == []


def test_builder_sorts_by_turnover_desc_and_caps_at_max():
    stocks = [make_stock(str(i)) for i in range(60)]
    prices = [
        make_price(
            str(i),
            close=Decimal("44.65"),
            ref=Decimal("40.60"),
            turnover=Decimal(str(1_000_000 * (i + 1))),
        )
        for i in range(60)
    ]
    builder = CandidateBuilder(minimum_turnover=Decimal("0"), maximum_candidates=50)
    candidates = builder.build(stocks, prices)

    assert len(candidates) == 50
    assert candidates[0].stock.stock_id == "59"  # highest turnover ranks first


def test_builder_excludes_non_limit_up_stock():
    stocks = [make_stock("1101")]
    prices = [
        make_price("1101", close=Decimal("44.60"), ref=Decimal("40.60"), turnover=Decimal("100000000")),
    ]
    builder = CandidateBuilder(minimum_turnover=Decimal("50000000"))
    assert builder.build(stocks, prices) == []
