from app.domain.models import Market, SecurityType, StockMaster
from app.domain.scoring import ScoredStock
from app.reports.report_builder import build_report_stocks


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
        "1234": StockMaster(
            stock_id="1234",
            stock_name="Example Corp A",
            market=Market.TWSE,
            security_type=SecurityType.COMMON_STOCK,
        ),
        "5678": StockMaster(
            stock_id="5678",
            stock_name="Example Corp B",
            market=Market.TWSE,
            security_type=SecurityType.COMMON_STOCK,
        ),
    }

    views = build_report_stocks(top_five=scored, stock_master=stock_master)

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
    views = build_report_stocks(top_five=scored, stock_master={})
    assert views[0].stock_name == "9999"
