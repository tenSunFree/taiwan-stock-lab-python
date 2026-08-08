import datetime as dt

from app.reports.text_renderer import (
    DISCLAIMER,
    ReportStockView,
    render_daily_report,
    render_no_qualified_stock_report,
    top_factors,
    utf16_length,
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
        stock_name="範例公司",
        total_score=84.2,
        data_completeness=0.96,
        top_factor_names=("流動性", "基本面"),
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
    banned = ["必買", "明牌", "保證獲利", "最佳買點", "跟單", "穩賺"]
    for phrase in banned:
        assert phrase not in report


def test_report_includes_stock_info_and_risk_text():
    stock = ReportStockView(
        rank=1,
        stock_id="1234",
        stock_name="範例公司",
        total_score=84.2,
        data_completeness=0.96,
        top_factor_names=("流動性",),
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
    assert "範例公司" in report
    assert "1234" in report
    assert "84.20" in report
    assert "近 5 日累積漲幅偏高" in report


def test_no_qualified_stock_report_still_sends_disclaimer():
    report = render_no_qualified_stock_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        total_limit_up_count=5,
        strategy_version="rule-v1.0.0",
    )
    assert DISCLAIMER in report
    assert "今日無符合資料完整度門檻的候選股" in report


def test_top_factors_picks_highest_scores_only():
    factor_scores = {
        "liquidity": 90.0,
        "momentum": 40.0,
        "fundamental": 85.0,
        "risk_quality": None,
    }
    result = top_factors(factor_scores, limit=2)
    assert result == ("流動性", "基本面")


def test_utf16_length_matches_line_counting_rule():
    assert utf16_length("abc") == 3
    assert utf16_length("台股") == 2