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
        missing_factor_names=(),
        is_one_price_limit_up=False,
        factor_scores={
            "liquidity": 80.0,
            "volume_price": 75.0,
            "momentum": 70.0,
            "institutional": 50.0,
            "fundamental": 50.0,
            "risk_quality": 90.0,
        },
        volume_ratio_20d=2.4,
        risk_missing_inputs=(),
    )
    defaults.update(overrides)
    return ReportStockView(**defaults)


def _render(*stocks: ReportStockView, ranking_limit: int = 10) -> str:
    return render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=18,
        eligible_count=12,
        strategy_version="rule-v1.2.0",
        ranked_stocks=list(stocks),
        ranking_limit=ranking_limit,
    )


# --- Basic scaffolding ---------------------------------------------------


def test_report_contains_disclaimer():
    report = _render()
    assert DISCLAIMER in report


def test_report_never_contains_banned_phrases():
    report = _render(_make_stock_view(risk_flags=("HIGH_FIVE_DAY_RETURN",)))
    banned = ["必買", "明牌", "保證獲利", "最佳買點", "跟單", "穩賺"]
    for phrase in banned:
        assert phrase not in report


def test_report_includes_stock_info():
    report = _render(_make_stock_view())
    assert "範例公司" in report
    assert "1234" in report
    assert "84.20" in report


def test_report_uses_candidate_and_completeness_labels():
    report = render_daily_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=18,
        eligible_count=12,
        strategy_version="rule-v1.2.0",
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
        strategy_version="rule-v1.2.0",
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


# --- 今日排名 / 資料完整度 --------------------------------------------------


def test_report_shows_daily_rank_against_ranking_limit():
    """今日排名的分母直接用 render_daily_report 的 ranking_limit 參數，
    不是 ReportStockView 自己另外帶一份 —— 避免出現兩份 source of
    truth 互相不同步的狀況。"""
    report = _render(_make_stock_view(rank=1), ranking_limit=10)
    assert "今日排名：1 / 10" in report


def test_report_always_shows_data_completeness():
    """資料完整度直接影響綜合分數的可信度（缺因子時分數是在「有拿到
    資料的因子」裡重新正規化算出來的），所以永遠顯示，不因為新排版
    就拿掉。"""
    report = _render(_make_stock_view(data_completeness=0.90))
    assert "資料完整度：90%" in report


# --- 資料缺口（動態文案，取代寫死的舊字串） ----------------------------------


def test_report_missing_risk_quality_uses_actual_missing_inputs():
    """核心 regression test：昨天顯示的「注意／處置狀態尚未串接官方
    資料源」是寫死的舊文案，即使 is_attention/is_disposition 早就接
    好了也不會消失。修正後必須依 risk_missing_inputs 實際內容動態產生
    文字，且絕對不能再出現那句舊字串。"""
    report = _render(
        _make_stock_view(
            missing_factor_names=("risk_quality",),
            risk_missing_inputs=("is_managed", "consecutive_limit_up_days"),
        )
    )
    assert (
        "資料缺口：風險品質（全額交割／變更交易方法狀態、連續漲停天數"
        "尚未確認，暫無法完整評分）" in report
    )
    assert "注意／處置狀態尚未串接官方資料源" not in report


def test_report_missing_other_factor_uses_its_own_reason():
    report = _render(_make_stock_view(missing_factor_names=("institutional",)))
    assert "資料缺口：法人籌碼（法人買賣超資料缺失）" in report


def test_report_omits_data_gap_line_when_nothing_missing():
    report = _render(_make_stock_view(missing_factor_names=()))
    assert "資料缺口：" not in report


# --- 漲停結構 ---------------------------------------------------------------


def test_report_shows_one_price_limit_up_bullet():
    report = _render(_make_stock_view(is_one_price_limit_up=True))
    assert "・一字漲停" in report


def test_report_shows_non_one_price_limit_up_bullet():
    report = _render(_make_stock_view(is_one_price_limit_up=False))
    assert "・非一字漲停" in report
    assert "・一字漲停" not in report


def test_report_shows_volume_ratio_when_available():
    report = _render(_make_stock_view(volume_ratio_20d=2.4))
    assert "・成交量 2.4×20 日均量" in report


def test_report_shows_data_insufficient_when_volume_ratio_unavailable():
    report = _render(_make_stock_view(volume_ratio_20d=None))
    assert "・20 日量比：資料不足" in report


# --- 訊號 --------------------------------------------------------------------


def test_report_shows_signal_lights_including_volume_price():
    """regression test：volume_price 佔 20% 權重，不能在訊號區塊裡被
    漏掉（Phase A 草稿曾經漏掉這個因子）。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 90.0,
                "volume_price": 45.0,
                "momentum": 30.0,
                "institutional": None,
                "fundamental": 75.0,
                "risk_quality": None,
            }
        )
    )
    assert "🟢 流動性：強" in report
    assert "🟡 量價：普通" in report
    assert "🔴 動能：偏弱" in report
    assert "⚪ 籌碼：資料不足" in report
    assert "🟢 基本面：強" in report
    assert "⚪ 風險品質：資料不足" in report


# --- 監管狀態（tri-state：True 標記 / False 官方確認正常 / None 未知） ---------


def test_attention_confirmed_clean_shows_normal():
    report = _render(_make_stock_view(risk_flags=(), risk_missing_inputs=()))
    assert "✅ 注意股：正常" in report


def test_attention_unknown_is_not_shown_as_normal():
    """最關鍵的語意測試：官方確認正常（False）跟根本不知道（None）
    絕對不能被顯示成同一件事。"""
    report = _render(
        _make_stock_view(risk_flags=(), risk_missing_inputs=("is_attention",))
    )
    assert "⚪ 注意股：待確認" in report
    assert "✅ 注意股：正常" not in report


def test_attention_flagged_shows_reason():
    report = _render(
        _make_stock_view(risk_flags=("ATTENTION_STOCK",), attention_reason="近期異常")
    )
    assert "⚠️ 注意股：注意中（近期異常）" in report


def test_attention_flagged_without_reason_falls_back_to_plain_label():
    report = _render(_make_stock_view(risk_flags=("ATTENTION_STOCK",)))
    assert "⚠️ 注意股：注意中（" not in report
    assert "⚠️ 注意股：注意中" in report


def test_disposition_confirmed_clean_shows_normal():
    report = _render(_make_stock_view(risk_flags=(), risk_missing_inputs=()))
    assert "✅ 處置股：正常" in report


def test_disposition_unknown_is_not_shown_as_normal():
    report = _render(
        _make_stock_view(risk_flags=(), risk_missing_inputs=("is_disposition",))
    )
    assert "⚪ 處置股：待確認" in report
    assert "✅ 處置股：正常" not in report


def test_disposition_flagged_shows_period_and_reason():
    report = _render(
        _make_stock_view(
            risk_flags=("DISPOSITION_STOCK",),
            disposition_start_date=dt.date(2026, 8, 24),
            disposition_end_date=dt.date(2026, 8, 28),
            disposition_reason="連續三次",
        )
    )
    assert "🚨 處置股：處置中" in report
    assert "處置期間：2026/08/24～2026/08/28" in report
    assert "處置原因：連續三次" in report


def test_managed_confirmed_clean_shows_no():
    report = _render(_make_stock_view(risk_flags=(), risk_missing_inputs=()))
    assert "✅ 全額交割／變更交易方法：否" in report


def test_managed_unknown_is_not_shown_as_no():
    report = _render(
        _make_stock_view(risk_flags=(), risk_missing_inputs=("is_managed",))
    )
    assert "⚪ 全額交割／變更交易方法：待確認" in report
    assert "✅ 全額交割／變更交易方法：否" not in report


def test_managed_flagged_shows_yes():
    report = _render(_make_stock_view(risk_flags=("MANAGED_STOCK",)))
    assert "🚨 全額交割／變更交易方法：是" in report


# --- 主要風險 ----------------------------------------------------------------


def test_primary_risks_always_include_base_risks():
    report = _render(_make_stock_view(risk_flags=()))
    assert "・隔日追價風險" in report
    assert "・開板風險" in report


def test_primary_risks_add_flag_driven_labels_without_dropping_base_risks():
    """regression test：加了額外的風險標籤不能把漲停股本來就有的基本
    風險（隔日追價、開板）擠掉。"""
    report = _render(_make_stock_view(risk_flags=("HIGH_FIVE_DAY_RETURN",)))
    assert "・隔日追價風險" in report
    assert "・開板風險" in report
    assert "・短線過熱" in report


def test_primary_risks_do_not_duplicate_regulatory_flags():
    """ATTENTION_STOCK/DISPOSITION_STOCK/MANAGED_STOCK 已經在監管狀態
    區塊完整顯示，主要風險區塊不需要重複列出。"""
    report = _render(_make_stock_view(risk_flags=("DISPOSITION_STOCK",)))
    assert "・處置股" not in report


# --- 模型說明 ------------------------------------------------------------------


def test_report_model_explanation_reflects_new_template():
    report = _render(_make_stock_view())
    assert "模型說明" in report
    assert "rule-v1.2.0" in report
    assert "不代表預測報酬率、上漲機率或目標價" in report
    assert "「訊號」依各因子的標準化分數區間呈現" in report
    assert "歷史分位及 T+1／T+5 統計尚未納入目前版本" in report
    # old text-v5 section is gone
    assert "「主要得分來源」" not in report


# --- No-qualified-stock report (unchanged behavior) --------------------------


def test_no_qualified_stock_report_still_sends_disclaimer():
    report = render_no_qualified_stock_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=5,
        strategy_version="rule-v1.2.0",
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
        strategy_version="rule-v1.2.0",
        ranking_limit=15,
    )
    assert "暫無 Top 15 名單" in report


# --- Standalone utilities (unchanged) -----------------------------------------


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
