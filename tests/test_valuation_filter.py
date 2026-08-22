import datetime as dt
from decimal import Decimal

import pytest

from app.domain.candidate_builder import Candidate
from app.domain.limit_up import LimitUpResult, LimitUpSource
from app.domain.models import DailyPrice, Market, SecurityType, StockMaster
from app.domain.models import StockValuation
from app.domain.valuation_filter import filter_candidates_by_pe

TRADING_DATE = dt.date(2026, 8, 7)


def make_candidate(stock_id: str) -> Candidate:
    stock = StockMaster(
        stock_id=stock_id,
        stock_name=f"Stock{stock_id}",
        market=Market.TWSE,
        security_type=SecurityType.COMMON_STOCK,
    )
    price = DailyPrice(
        trading_date=TRADING_DATE,
        stock_id=stock_id,
        reference_price=Decimal("100"),
        open_price=Decimal("100"),
        high_price=Decimal("110"),
        low_price=Decimal("100"),
        close_price=Decimal("110"),
        volume=1_000_000,
        turnover=Decimal("100000000"),
    )
    limit_up = LimitUpResult(
        is_close_limit_up=True,
        has_touched_limit_up=True,
        limit_up_price=Decimal("110"),
        limit_up_source=LimitUpSource.CALCULATED,
        reason="test fixture",
    )
    return Candidate(stock=stock, price=price, limit_up=limit_up)


def make_valuation(stock_id: str, pe_ratio: Decimal | None) -> StockValuation:
    return StockValuation(
        trading_date=TRADING_DATE, stock_id=stock_id, pe_ratio=pe_ratio
    )


# --- boundary table: 0 < P/E <= 20 -----------------------------------------


@pytest.mark.parametrize(
    "pe_ratio",
    [
        Decimal("-1"),
        Decimal("0"),
        Decimal("20.01"),
        Decimal("25"),
    ],
)
def test_excludes_pe_outside_valid_range(pe_ratio):
    candidate = make_candidate("1101")

    result = filter_candidates_by_pe(
        [candidate],
        {"1101": make_valuation("1101", pe_ratio)},
        maximum_pe_ratio=Decimal("20"),
    )

    assert result == []


@pytest.mark.parametrize(
    "pe_ratio",
    [
        Decimal("0.01"),
        Decimal("15"),
        Decimal("19.99"),
        Decimal("20"),  # inclusive: "不高於 20 倍" means <= 20
    ],
)
def test_keeps_pe_within_valid_range(pe_ratio):
    candidate = make_candidate("1101")

    result = filter_candidates_by_pe(
        [candidate],
        {"1101": make_valuation("1101", pe_ratio)},
        maximum_pe_ratio=Decimal("20"),
    )

    assert [c.stock.stock_id for c in result] == ["1101"]


def test_excludes_none_pe():
    """
    None means the source did not publish a P/E for this stock
    (typically zero/negative trailing EPS) — fail-closed, not
    "assume it passes."
    """
    candidate = make_candidate("1101")

    result = filter_candidates_by_pe(
        [candidate],
        {"1101": make_valuation("1101", None)},
        maximum_pe_ratio=Decimal("20"),
    )

    assert result == []


def test_excludes_candidate_without_valuation_record():
    """A stock_id with no entry in valuations_by_stock at all (e.g.
    the whole-market snapshot simply didn't include it) is excluded
    the same as an explicit None — missing data is missing data."""
    candidate = make_candidate("1101")

    result = filter_candidates_by_pe([candidate], {}, maximum_pe_ratio=Decimal("20"))

    assert result == []


def test_preserves_candidate_order():
    first = make_candidate("1101")
    second = make_candidate("2330")
    third = make_candidate("2317")

    result = filter_candidates_by_pe(
        [first, second, third],
        {
            "1101": make_valuation("1101", Decimal("15")),
            "2330": make_valuation("2330", Decimal("30")),  # excluded, > 20
            "2317": make_valuation("2317", Decimal("10")),
        },
        maximum_pe_ratio=Decimal("20"),
    )

    assert [c.stock.stock_id for c in result] == ["1101", "2317"]


def test_empty_candidate_list_returns_empty_list():
    result = filter_candidates_by_pe([], {}, maximum_pe_ratio=Decimal("20"))
    assert result == []


def test_rejects_non_positive_maximum_pe_ratio():
    with pytest.raises(ValueError, match="maximum_pe_ratio must be positive"):
        filter_candidates_by_pe([], {}, maximum_pe_ratio=Decimal("0"))

    with pytest.raises(ValueError, match="maximum_pe_ratio must be positive"):
        filter_candidates_by_pe([], {}, maximum_pe_ratio=Decimal("-5"))
