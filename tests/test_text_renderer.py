import datetime as dt

from app.reports.text_renderer import (
    DISCLAIMER,
    ReportStockView,
    render_daily_report,
    render_no_qualified_stock_report,
    top_factors,
)

TRADING_DATE = dt.date(2026, 8, 7)


def test_report_contains_disclaimer():
    report = render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        total_limit_up_count=18,
        qualified_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=[],
    )
    assert DISCLAIMER in report


def test_report_never_contains_banned_phrases():
    stock = ReportStockView(
        rank=1,
        stock_id="1234",
        stock_name="Example Corp",
        total_score=84.2,
        data_completeness=0.96,
        top_factor_names=("liquidity", "fundamentals"),
        risk_flags=("HIGH_FIVE_DAY_RETURN",),
    )
    report = render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        total_limit_up_count=18,
        qualified_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=[stock],
    )
    banned = [
        "must buy",
        "hot tip",
        "best pick",
        "guaranteed profit",
        "follow this trade",
    ]
    lowered = report.lower()
    for phrase in banned:
        assert phrase not in lowered


def test_report_includes_stock_info_and_risk_text():
    stock = ReportStockView(
        rank=1,
        stock_id="1234",
        stock_name="Example Corp",
        total_score=84.2,
        data_completeness=0.96,
        top_factor_names=("liquidity",),
        risk_flags=("HIGH_FIVE_DAY_RETURN",),
    )
    report = render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        total_limit_up_count=18,
        qualified_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=[stock],
    )
    assert "Example Corp" in report
    assert "1234" in report
    assert "84.2" in report
    assert "elevated 5-day cumulative return" in report


def test_no_qualified_stock_report_still_sends_disclaimer():
    report = render_no_qualified_stock_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        total_limit_up_count=5,
        strategy_version="rule-v1.0.0",
    )
    assert DISCLAIMER in report
    assert "No candidates passed the data-completeness threshold" in report


def test_top_factors_picks_highest_scores_only():
    factor_scores = {
        "liquidity": 90.0,
        "momentum": 40.0,
        "fundamental": 85.0,
        "risk_quality": None,
    }
    result = top_factors(factor_scores, limit=2)
    assert result == ("liquidity", "fundamentals")
