import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.candidate_builder import Candidate
from app.domain.limit_up import LimitUpResult, LimitUpSource
from app.domain.models import DailyPrice, Market, SecurityType, StockMaster
from app.domain.scoring import ScoredStock
from app.reports.report_builder import build_report_stocks

TRADING_DATE = dt.date(2026, 8, 7)


def _make_stock(
    stock_id: str = "1234", stock_name: str = "Example Corp A"
) -> StockMaster:
    return StockMaster(
        stock_id=stock_id,
        stock_name=stock_name,
        market=Market.TWSE,
        security_type=SecurityType.COMMON_STOCK,
    )


def _make_candidate(
    *,
    stock_id: str = "1234",
    stock_name: str = "Example Corp A",
    open_price: str = "100",
    high_price: str = "110",
    low_price: str = "100",
    close_price: str = "110",
    reference_price: str = "100",
    limit_up_price: str = "110",
) -> Candidate:
    stock = _make_stock(stock_id, stock_name)
    price = DailyPrice(
        trading_date=TRADING_DATE,
        stock_id=stock_id,
        reference_price=Decimal(reference_price),
        open_price=Decimal(open_price),
        high_price=Decimal(high_price),
        low_price=Decimal(low_price),
        close_price=Decimal(close_price),
        volume=1_000_000,
        turnover=Decimal("100000000"),
    )
    limit_up = LimitUpResult(
        is_close_limit_up=True,
        has_touched_limit_up=True,
        limit_up_price=Decimal(limit_up_price),
        limit_up_source=LimitUpSource.CALCULATED,
        reason="test fixture",
    )
    return Candidate(stock=stock, price=price, limit_up=limit_up)


def test_build_report_stocks_maps_names_and_rank():
    scored = [
        ScoredStock(
            stock_id="1234",
            total_score=84.2,
            data_completeness=0.96,
            factor_scores={"liquidity": 90.0, "momentum": 40.0, "fundamental": 85.0},
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        ),
        ScoredStock(
            stock_id="5678",
            total_score=80.4,
            data_completeness=0.91,
            factor_scores={"institutional": 75.0, "liquidity": 70.0},
            risk_flags=(),
        ),
    ]
    stock_master = {
        "1234": _make_stock("1234", "Example Corp A"),
        "5678": _make_stock("5678", "Example Corp B"),
    }
    candidates = {
        "1234": _make_candidate(stock_id="1234", stock_name="Example Corp A"),
        "5678": _make_candidate(stock_id="5678", stock_name="Example Corp B"),
    }

    views = build_report_stocks(
        top_five=scored, stock_master=stock_master, candidates=candidates
    )

    assert [v.rank for v in views] == [1, 2]
    assert views[0].stock_name == "Example Corp A"
    assert views[1].stock_name == "Example Corp B"
    assert views[0].top_factor_names == ("流動性", "基本面")


def test_build_report_stocks_falls_back_to_stock_id_when_name_missing():
    scored = [
        ScoredStock(
            stock_id="9999",
            total_score=70.0,
            data_completeness=1.0,
            factor_scores={},
            risk_flags=(),
        )
    ]
    candidates = {"9999": _make_candidate(stock_id="9999")}
    views = build_report_stocks(top_five=scored, stock_master={}, candidates=candidates)
    assert views[0].stock_name == "9999"


def test_build_report_stocks_computes_close_price_and_change_percent():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={
                "liquidity": 90.0,
                "volume_price": 80.0,
                "momentum": 75.0,
                "institutional": 70.0,
                "fundamental": 85.0,
                "risk_quality": None,
            },
            risk_flags=(),
        )
    ]
    candidate = _make_candidate(
        stock_id="1101",
        open_price="41.00",
        high_price="44.65",
        low_price="40.80",
        close_price="44.65",
        reference_price="40.60",
        limit_up_price="44.65",
    )

    result = build_report_stocks(
        top_five=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
    )

    assert result[0].close_price == Decimal("44.65")
    assert result[0].change_percent == pytest.approx(9.975369458128078)
    # open (41.00) != close (44.65), so this is NOT a one-price limit-up
    assert result[0].is_one_price_limit_up is False
    assert result[0].missing_factor_names == ("risk_quality",)


def test_build_report_stocks_detects_one_price_limit_up():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={"liquidity": 90.0, "risk_quality": None},
            risk_flags=("ONE_PRICE_LIMIT_UP",),
        )
    ]
    # open == high == low == close == limit_up_price
    candidate = _make_candidate(
        stock_id="1101",
        open_price="110",
        high_price="110",
        low_price="110",
        close_price="110",
        reference_price="100",
        limit_up_price="110",
    )

    result = build_report_stocks(
        top_five=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
    )

    assert result[0].is_one_price_limit_up is True


def test_build_report_stocks_leaves_change_percent_none_without_reference_price():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=(),
        )
    ]
    candidate = _make_candidate(stock_id="1101")
    candidate = replace(candidate, price=replace(candidate.price, reference_price=None))

    result = build_report_stocks(
        top_five=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
    )

    assert result[0].change_percent is None


def test_build_report_stocks_raises_on_missing_candidate():
    stock = _make_stock("1101", "測試水泥")
    scored = ScoredStock(
        stock_id="1101",
        total_score=80.0,
        data_completeness=0.90,
        factor_scores={},
        risk_flags=(),
    )

    with pytest.raises(RuntimeError, match="does not exist in CandidateBuilder output"):
        build_report_stocks(
            top_five=[scored], stock_master={"1101": stock}, candidates={}
        )
