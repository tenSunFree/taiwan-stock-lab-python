import datetime as dt
from decimal import Decimal

from app.reports.text_renderer import (
    DISCLAIMER,
    ReportStockView,
    render_daily_report,
    render_no_qualified_stock_report,
    top_factors,
    utf16_length,
)

TRADING_DATE = dt.date(2026, 8, 7)


def _make_stock_view(**overrides) -> ReportStockView:
    defaults = dict(
        rank=1,
        stock_id="1234",
        stock_name="範例公司",
        total_score=84.2,
        data_completeness=0.90,
        top_factor_names=("流動性", "動能"),
        risk_flags=(),
        close_price=Decimal("177.5"),
        change_percent=9.91,
        missing_factor_names=("risk_quality",),
        is_one_price_limit_up=False,
    )
    defaults.update(overrides)
    return ReportStockView(**defaults)


def _render(*stocks: ReportStockView) -> str:
    return render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=18,
        eligible_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=list(stocks),
    )


def test_report_contains_disclaimer():
    report = _render()
    assert DISCLAIMER in report


def test_report_never_contains_banned_phrases():
    report = _render(_make_stock_view(risk_flags=("HIGH_FIVE_DAY_RETURN",)))
    banned = ["必買", "明牌", "保證獲利", "最佳買點", "跟單", "穩賺"]
    for phrase in banned:
        assert phrase not in report


def test_report_includes_stock_info_and_risk_text():
    report = _render(
        _make_stock_view(
            top_factor_names=("流動性",),
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        )
    )
    assert "範例公司" in report
    assert "1234" in report
    assert "84.20" in report
    assert "近 5 日累積漲幅偏高" in report


def test_report_uses_candidate_and_completeness_labels():
    """
    Regression test for the count-semantics fix: candidate_count is
    the CandidateBuilder pool size (already turnover-filtered and
    capped at maximum_candidates), NOT a raw whole-market limit-up
    count — and eligible_count is specifically "cleared the
    completeness gate," not just "scored."
    """
    report = render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=18,
        eligible_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=[],
    )
    assert "進入候選池：18 檔" in report
    assert "通過資料完整度門檻：12 檔" in report
    assert "展示範圍：綜合分數 Top 10" in report


def test_report_uses_custom_ranking_limit_in_display_line():
    report = render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=18,
        eligible_count=12,
        strategy_version="rule-v1.0.0",
        ranked_stocks=[],
        ranking_limit=15,
    )
    assert "展示範圍：綜合分數 Top 15" in report


def test_report_shows_close_price_and_positive_change_percent():
    report = _render(
        _make_stock_view(close_price=Decimal("177.5"), change_percent=9.91)
    )
    assert "收盤價：177.5 元" in report
    assert "漲幅：+9.91%" in report


def test_report_shows_negative_change_percent_with_sign():
    report = _render(_make_stock_view(change_percent=-2.15))
    assert "漲幅：-2.15%" in report


def test_report_omits_price_lines_when_unavailable():
    report = _render(_make_stock_view(close_price=None, change_percent=None))
    assert "收盤價：" not in report
    assert "漲幅：" not in report


def test_report_shows_missing_data_breakdown_with_reason():
    report = _render(_make_stock_view(missing_factor_names=("risk_quality",)))
    assert "缺失資料：" in report
    assert "風險品質（注意／處置狀態尚未串接官方資料源，暫無法評分）" in report


def test_report_omits_missing_data_line_when_complete():
    report = _render(_make_stock_view(missing_factor_names=()))
    assert "缺失資料：" not in report


def test_report_shows_one_price_limit_up_pattern_section():
    report = _render(_make_stock_view(is_one_price_limit_up=True))
    assert "型態特徵：" in report
    assert "・一字漲停" in report


def test_report_omits_pattern_section_when_not_one_price_limit_up():
    report = _render(_make_stock_view(is_one_price_limit_up=False))
    assert "型態特徵：" not in report


def test_report_uses_renamed_section_headers():
    report = _render(_make_stock_view())
    assert "主要得分來源：" in report
    assert "風險提示：" in report
    # old text-v1 headers must be gone
    assert "主要優勢：" not in report
    assert "風險：\n" not in report


def test_report_default_risk_text_when_no_flags():
    report = _render(_make_stock_view(risk_flags=()))
    assert "今日收盤漲停，隔日追高、開板及價格波動風險偏高" in report


def test_report_includes_model_explanation():
    report = _render(_make_stock_view())
    assert "模型說明" in report
    assert "rule-v1.0.0" in report
    assert "不代表預測報酬率、上漲機率或目標價" in report


def test_no_qualified_stock_report_still_sends_disclaimer():
    report = render_no_qualified_stock_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=5,
        strategy_version="rule-v1.0.0",
    )
    assert DISCLAIMER in report
    assert "今日無符合資料完整度門檻的候選股" in report
    assert "進入候選池：5 檔" in report
    assert "暫無 Top 10 名單" in report


def test_no_qualified_stock_report_uses_custom_ranking_limit():
    report = render_no_qualified_stock_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=5,
        strategy_version="rule-v1.0.0",
        ranking_limit=15,
    )
    assert "暫無 Top 15 名單" in report


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
    # supplementary-plane character (emoji) — encodes as a UTF-16
    # surrogate pair (2 code units), but Python's len() would count
    # it as 1 character. This is the case that actually distinguishes
    # a correct UTF-16-based implementation from an incorrect one
    # based on len(text).
    assert len("😀") == 1  # sanity check: Python counts this as 1 code point
    assert utf16_length("😀") == 2
