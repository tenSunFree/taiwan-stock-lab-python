import datetime as dt
from decimal import Decimal

import pytest

from app.reports.text_renderer import (
    DISCLAIMER,
    MAX_LINE_TEXT_UTF16_UNITS,
    ReportStockView,
    render_daily_report,
    render_daily_report_messages,
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


def test_report_shows_daily_rank_against_actual_stock_count():
    """今日排名的分母是「今天實際入榜的股票數」（len(ranked_stocks)），
    不是 render_daily_report 的 ranking_limit 參數本身——這兩個數字
    在「符合門檻的股票數 < ranking_limit」時會不一樣（例如今天只有 5
    檔符合門檻，即使 ranking_limit 設定為 10），分母必須反映讀者眼前
    這份清單實際的股票數，不能顯示一個清單裡根本找不到對應排名的分母
    （例如清單只有 5 檔，卻寫「1 / 10」，讀者會找不到 6~10 名在哪）。"""
    report = _render(_make_stock_view(rank=1), ranking_limit=10)
    assert "今日排名：1 / 1" in report


def test_report_shows_daily_rank_denominator_matches_stocks_actually_listed():
    """更貼近真實情境的回歸測試：即使 ranking_limit 設定為 10，只要
    今天實際只有 5 檔股票符合門檻進入 ranked_stocks，每一檔的「今日
    排名」分母都必須是 5，不是 10——這正是使用者回報過的真實案例
    （通過資料完整度門檻：5 檔，但排名卻顯示 1 / 10 的落差）。"""
    stocks = [_make_stock_view(rank=i) for i in range(1, 6)]
    report = _render(*stocks, ranking_limit=10)
    for rank in range(1, 6):
        assert f"今日排名：{rank} / 5" in report
    assert "/ 10" not in report


def test_report_top_display_line_still_reflects_configured_ranking_limit():
    """「顯示 Top N」與「展示範圍：綜合分數 Top N」這兩行描述的是設定
    的顯示上限本身，跟今天實際入榜幾檔無關，不應該被上面那個分母的
    修正影響。"""
    stocks = [_make_stock_view(rank=i) for i in range(1, 6)]
    report = _render(*stocks, ranking_limit=10)
    assert "✅ 顯示 Top 10" in report
    assert "展示範圍：綜合分數 Top 10" in report


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
    漏掉（Phase A 草稿曾經漏掉這個因子）。這個 fixture 的 risk_flags
    是空的（沒有 HIGH_FIVE_DAY_RETURN），所以 momentum=30.0 應該仍然
    顯示一般的「偏弱」，不是「漲多過熱」——見下面
    test_overheated_momentum_is_not_described_as_weak 測試「有觸發
    旗標」的另一種情況。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 90.0,
                "volume_price": 45.0,
                "momentum": 30.0,
                "institutional": None,
                "fundamental": 75.0,
                "risk_quality": None,
            },
            revenue_yoy=0.12,
        )
    )
    assert "🟢 流動性：強" in report
    assert "🟡 量價：普通" in report
    assert "🔴 動能：偏弱" in report
    assert "⚪ 籌碼：資料不足" in report
    assert "🟢 基本面：強" in report
    assert "⚪ 風險品質：資料不足" in report


def test_overheated_momentum_is_not_described_as_weak():
    """
    核心 regression test，直接對應「世紀* 5 日大漲 60.9%，但顯示
    『動能：偏弱』」這個真實案例：bounded_momentum_score 是非單調
    評分（累積漲幅過高會被扣分，不是自動更強），所以低分不代表動能
    疲弱，也可能代表已經漲多過熱。當 HIGH_FIVE_DAY_RETURN 這個既有
    風險旗標被觸發時，動能訊號的文字必須明確改成「漲多過熱」，不能
    再用會誤導成「近期沒有動能」的通用「偏弱」字樣。
    """
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 60.0,
                "momentum": 20.0,
                "institutional": 50.0,
                "fundamental": 50.0,
                "risk_quality": 80.0,
            },
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        )
    )
    assert "🔴 動能：漲多過熱" in report
    assert "🔴 動能：偏弱" not in report


def test_momentum_missing_data_still_shows_insufficient_even_with_flag():
    """即使 HIGH_FIVE_DAY_RETURN 被觸發，如果 momentum 分數本身是
    None（真的沒有資料可算），也不能顯示「漲多過熱」——那是對一個不
    存在的分數做出判斷。理論上這個組合不該發生（兩者都依賴同一個
    return_5d 是否為 None），但顯式測試這個防禦順序，確保未來重構
    不會不小心把判斷順序顛倒。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 60.0,
                "momentum": None,
                "institutional": 50.0,
                "fundamental": 50.0,
                "risk_quality": 80.0,
            },
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        )
    )
    assert "⚪ 動能：資料不足" in report
    assert "🔴 動能：漲多過熱" not in report


def test_high_momentum_with_flag_still_shows_strong_not_overheated():
    """CodeRabbit review comment（PR #29）：RiskPolicyConfig.excessive_return_5d
    是獨立於 bounded_momentum_score 門檻之外、可另外調整的設定值，所以
    HIGH_FIVE_DAY_RETURN 完全可能在 momentum 分數依然是 70 分以上
    （「強」）時被觸發（例如 excessive_return_5d 被調得比實際會讓分數
    倒扣的漲幅門檻還低）。這種情況下動能其實是真的強，不該被「漲多
    過熱」蓋掉——那樣反而是這個函式原本想避免的「誤導」本身。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 60.0,
                "momentum": 75.0,
                "institutional": 50.0,
                "fundamental": 50.0,
                "risk_quality": 80.0,
            },
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        )
    )
    assert "🟢 動能：強" in report
    assert "動能：漲多過熱" not in report


def test_moderate_momentum_with_flag_still_shows_normal_not_overheated():
    """同上一個 test 的分界情況：momentum=50.0 落在「普通」區間
    （>= 40 且 < 70），即使 HIGH_FIVE_DAY_RETURN 同時被觸發，也不該
    被覆寫成「漲多過熱」。過熱覆寫只在分數本身已經落入「偏弱」區間
    （< 40，見 _WEAK_SIGNAL_THRESHOLD）時才有意義。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 60.0,
                "momentum": 50.0,
                "institutional": 50.0,
                "fundamental": 50.0,
                "risk_quality": 80.0,
            },
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        )
    )
    assert "🟡 動能：普通" in report
    assert "動能：漲多過熱" not in report


# --- 法人籌碼（display-only tri-state，獨立於 institutional 評分因子） --------


def test_institutional_net_buy_positive_shows_yes():
    report = _render(_make_stock_view(institutional_net_buy_3d_positive=True))
    assert "✅ 近 3 個交易日累積買超 > 0：是" in report


def test_institutional_net_buy_non_positive_shows_no():
    report = _render(_make_stock_view(institutional_net_buy_3d_positive=False))
    assert "❌ 近 3 個交易日累積買超 > 0：否" in report


def test_institutional_net_buy_unknown_shows_insufficient_data():
    """預設值（未提供時）與明確傳入 None 都必須顯示「資料不足」，
    不能因為 Python 的 falsy 判斷把 None 誤判成 False（否）——這是
    這個 tri-state 欄位存在的原因，混淆兩者會讓「不知道」被誤報成
    「確認沒有買超」。"""
    report = _render(_make_stock_view(institutional_net_buy_3d_positive=None))
    assert "⚪ 近 3 個交易日累積買超 > 0：資料不足" in report
    assert "✅ 近 3 個交易日累積買超" not in report
    assert "❌ 近 3 個交易日累積買超" not in report


def test_institutional_net_buy_flow_block_also_forces_unknown_when_cutoff_is_stale():
    """CodeRabbit regression test：「法人籌碼」tri-state 區塊跟「訊號」
    區塊裡的籌碼因子共用同一組法人資料，必須套用一樣的 T-1 stale
    防護。即使 institutional_net_buy_3d_positive 本身仍然有值
    （True，理論上對應「是」），只要 institutional_data_cutoff 明確
    表示 T-1 尚未確認，這個區塊也必須強制顯示「資料不足」，不能信任
    這個未確認的舊/不一致資料畫出「是」——否則「訊號」區塊顯示
    ⚪ 資料不足，但「法人籌碼」區塊卻顯示 ✅ 是，會是自相矛盾的報表。"""
    from app.domain.institutional_flow_builder import InstitutionalDataCutoff

    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 8, 6), confirmed_as_of_date=None
    )
    report = _render(
        _make_stock_view(
            institutional_net_buy_3d_positive=True,
            institutional_data_cutoff=cutoff,
        )
    )
    assert "⚪ 近 3 個交易日累積買超 > 0：資料不足" in report
    assert "✅ 近 3 個交易日累積買超" not in report
    assert "法人資料截止：T-1（2026/08/06）資料尚未確認" in report


def test_institutional_net_buy_flow_block_shows_cutoff_line_when_current():
    """正常情況（T-1 已確認）下，「法人籌碼」區塊也要跟「訊號」區塊
    一樣印出明確的資料截止日，而不是只有「訊號」區塊有這個揭露。"""
    from app.domain.institutional_flow_builder import InstitutionalDataCutoff

    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 8, 6),
        confirmed_as_of_date=dt.date(2026, 8, 6),
    )
    report = _render(
        _make_stock_view(
            institutional_net_buy_3d_positive=True,
            institutional_data_cutoff=cutoff,
        )
    )
    assert "✅ 近 3 個交易日累積買超 > 0：是" in report
    assert "法人資料截止：T-1（2026/08/06）" in report


def test_institutional_net_buy_is_independent_of_institutional_score():
    """Absolute institutional signal、candidate-pool relative score，
    以及 3-day institutional display signal 是三件不同的事：即使
    Absolute Signal 為 🔴 偏弱（近 5 日淨買超比為負），近 3 日累積
    買超一樣可能是正的——5 日 ratio 與 3 日 cumulative net-buy sign
    本來就是不同的計算窗口（例如：前兩日大量賣超、最近三日小幅轉買，
    5 日合計仍可能為負，3 日合計卻是正），兩者不應互相覆蓋或矛盾地
    被合併顯示。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 20.0,
                "fundamental": 50.0,
                "risk_quality": 90.0,
            },
            institutional_net_buy_ratio_5d=-0.02,
            institutional_net_buy_3d_positive=True,
        )
    )
    assert "🔴 籌碼：偏弱" in report
    assert "✅ 近 3 個交易日累積買超 > 0：是" in report


# --- 技術面（display-only tri-state，獨立於 momentum 評分因子） --------------


def test_technical_signal_shows_yes():
    report = _render(_make_stock_view(technical_low_with_rising_signal=True))
    assert "✅ 低檔且具起漲訊號：是" in report


def test_technical_signal_shows_no():
    report = _render(_make_stock_view(technical_low_with_rising_signal=False))
    assert "❌ 低檔且具起漲訊號：否" in report


def test_technical_signal_shows_insufficient_data():
    """預設值（未提供時）與明確傳入 None 都必須顯示「資料不足」，
    不能因為 Python 的 falsy 判斷把 None 誤判成 False（否）——理由跟
    法人籌碼那個 tri-state 欄位完全一樣。"""
    report = _render(_make_stock_view(technical_low_with_rising_signal=None))
    assert "⚪ 低檔且具起漲訊號：資料不足" in report
    assert "✅ 低檔且具起漲訊號" not in report
    assert "❌ 低檔且具起漲訊號" not in report


def test_technical_signal_is_independent_of_momentum_score():
    """技術面區塊的 True/False 與「訊號」區塊裡 momentum 因子的分數是
    兩件獨立的事：即使 momentum 評分因子顯示「漲多過熱」（近期漲幅已
    過熱、分數偏低），技術面訊號本身仍是用完全不同的價格區間／均線
    計算方式得出，兩者不應互相覆蓋或矛盾地被合併顯示。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 20.0,
                "institutional": 60.0,
                "fundamental": 50.0,
                "risk_quality": 90.0,
            },
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
            technical_low_with_rising_signal=True,
        )
    )
    assert "動能：漲多過熱" in report
    assert "✅ 低檔且具起漲訊號：是" in report


# --- 基本面（display-only tri-state：營收 OR EPS，獨立於 fundamental 評分因子）


def test_fundamental_growth_sustained_shows_yes():
    """營收單獨成立（EPS 未提供/None）就足以讓合併後的頭行顯示「是」——
    OR 的語意：任一邊確定為 True，結果就是 True。"""
    report = _render(_make_stock_view(fundamental_growth_sustained=True))
    assert "✅ 營收或 EPS YoY ≥ 10%，且具持續性：是" in report
    assert "　營收 YoY ≥ 10%，且具持續性：是" in report
    assert "　EPS YoY ≥ 10%，且具持續性：資料不足" in report


def test_eps_growth_sustained_alone_also_satisfies_combined_headline():
    """反過來：營收確定為 False，但 EPS 確定為 True，合併結果仍然是
    「是」——不能因為營收沒過就把整體判成否，這正是 OR 語意存在的理由。"""
    report = _render(
        _make_stock_view(fundamental_growth_sustained=False, eps_growth_sustained=True)
    )
    assert "✅ 營收或 EPS YoY ≥ 10%，且具持續性：是" in report
    assert "　營收 YoY ≥ 10%，且具持續性：否" in report
    assert "　EPS YoY ≥ 10%，且具持續性：是" in report


def test_fundamental_growth_not_sustained_shows_no():
    """只有當營收與 EPS 都確定為 False 時，合併頭行才會是「否」——
    單獨一邊 False、另一邊未知（None）不算數，見下面的 unknown 測試。"""
    report = _render(
        _make_stock_view(fundamental_growth_sustained=False, eps_growth_sustained=False)
    )
    assert "❌ 營收或 EPS YoY ≥ 10%，且具持續性：否" in report
    assert "　營收 YoY ≥ 10%，且具持續性：否" in report
    assert "　EPS YoY ≥ 10%，且具持續性：否" in report


def test_fundamental_growth_sustained_unknown_shows_insufficient_data():
    """兩者都是預設值（None）時，合併頭行與兩條子項都必須顯示「資料
    不足」，不能因為 Python 的 falsy 判斷把 None 誤判成 False（否）——
    理由跟法人籌碼／技術面那兩個 tri-state 欄位完全一樣。"""
    report = _render(_make_stock_view())
    assert "⚪ 營收或 EPS YoY ≥ 10%，且具持續性：資料不足" in report
    assert "　營收 YoY ≥ 10%，且具持續性：資料不足" in report
    assert "　EPS YoY ≥ 10%，且具持續性：資料不足" in report
    assert "✅ 營收或 EPS YoY ≥ 10%，且具持續性" not in report
    assert "❌ 營收或 EPS YoY ≥ 10%，且具持續性" not in report


def test_fundamental_growth_one_confirmed_false_one_unknown_stays_unconfirmed():
    """營收確定為 False，但 EPS 還是未知（None）——合併結果不能因為
    「至少有一邊確定」就順勢判成否；EPS 仍有可能是 True，所以頭行必須
    維持「資料不足」，不能提早下結論。"""
    report = _render(_make_stock_view(fundamental_growth_sustained=False))
    assert "⚪ 營收或 EPS YoY ≥ 10%，且具持續性：資料不足" in report
    assert "　營收 YoY ≥ 10%，且具持續性：否" in report
    assert "　EPS YoY ≥ 10%，且具持續性：資料不足" in report


def test_absolute_fundamental_signal_is_independent_of_growth_sustained_signal():
    """Absolute fundamental signal（最新月營收 YoY 的絕對值）、
    候選池相對分數，以及「營收或 EPS 持續性」這個獨立的 tri-state
    區塊，是三件不同的事：最新月營收 YoY 可以是 -5%（Absolute Signal
    因此為 🔴 偏弱），但 EPS 的持續性判斷仍可能是 True（不同的計算
    窗口、不同的資料來源）——兩者不應互相覆蓋或矛盾地被合併顯示。

    注意：不能用 revenue_yoy=-0.05 搭配
    fundamental_growth_sustained=True，因為 fundamental_growth_sustained
    本身的規則就要求最新月營收 YoY >= 10%（見
    app.domain.monthly_revenue_builder），兩者在正常資料中不可能同時
    成立。這裡改用 eps_growth_sustained=True 來證明同一件事：Absolute
    Fundamental Signal 不等於 Revenue/EPS Sustained Signal，也不等於
    Relative Fundamental Score。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 60.0,
                "fundamental": 20.0,
                "risk_quality": 90.0,
            },
            revenue_yoy=-0.05,
            fundamental_growth_sustained=False,
            eps_growth_sustained=True,
        )
    )
    assert "🔴 基本面：偏弱" in report
    assert "　EPS YoY ≥ 10%，且具持續性：是" in report


def test_progress_checklist_shows_fundamental_growth_as_done():
    """功能上線後，「📌 功能進度」清單裡的「基本面」項目要從 ⬜ 改成
    ✅，且文字要反映「營收或 EPS」的合併判斷已經上線，不再標註「EPS
    尚未串接」。"""
    report = _render(_make_stock_view())
    assert "✅ 基本面：營收或 EPS YoY ≥ 10%，且具持續性" in report
    assert "EPS 尚未串接" not in report
    assert "⬜ 基本面" not in report


# --- 監管狀態（tri-state：True 標記 / False 官方確認正常 / None 未知） ---------


def test_attention_confirmed_clean_shows_not_announced_today():
    report = _render(_make_stock_view(risk_flags=(), risk_missing_inputs=()))
    assert "✅ 今日公布注意：否" in report


def test_attention_unknown_is_not_shown_as_confirmed_no():
    """最關鍵的語意測試：官方確認正常（False）跟根本不知道（None）
    絕對不能被顯示成同一件事。"""
    report = _render(
        _make_stock_view(risk_flags=(), risk_missing_inputs=("is_attention",))
    )
    assert "⚪ 今日公布注意：待確認" in report
    assert "✅ 今日公布注意：否" not in report


def test_attention_flagged_shows_reason():
    report = _render(
        _make_stock_view(risk_flags=("ATTENTION_STOCK",), attention_reason="近期異常")
    )
    assert "⚠️ 今日公布注意：是（近期異常）" in report


def test_attention_flagged_without_reason_falls_back_to_plain_label():
    report = _render(_make_stock_view(risk_flags=("ATTENTION_STOCK",)))
    assert "⚠️ 今日公布注意：是（" not in report
    assert "⚠️ 今日公布注意：是" in report


def test_disposition_confirmed_clean_shows_not_active():
    report = _render(_make_stock_view(risk_flags=(), risk_missing_inputs=()))
    assert "✅ 目前處置：否" in report


def test_disposition_unknown_is_not_shown_as_confirmed_no():
    report = _render(
        _make_stock_view(risk_flags=(), risk_missing_inputs=("is_disposition",))
    )
    assert "⚪ 目前處置：待確認" in report
    assert "✅ 目前處置：否" not in report


def test_disposition_flagged_shows_period_reason_and_official_measure_notice():
    report = _render(
        _make_stock_view(
            risk_flags=("DISPOSITION_STOCK",),
            disposition_start_date=dt.date(2026, 8, 24),
            disposition_end_date=dt.date(2026, 8, 28),
            disposition_reason="連續三次",
        )
    )
    assert "🚨 目前處置：是" in report
    assert "處置期間：2026/08/24～2026/08/28" in report
    assert "處置原因：連續三次" in report
    assert "處置措施：請依交易所該次公告為準" in report


def test_attention_and_disposition_can_have_different_time_semantics():
    """
    核心 UX regression test：直接把這次修正發現的 domain semantics
    固定成測試案例。「今日公布注意：否」跟「目前處置：是」同時出現
    完全合法、不是矛盾——注意股是逐日公告（今天有沒有被公布），
    處置股是一段期間內的持續狀態（現在是否落在處置期間內），兩者
    回答的是不同時間軸上的問題，本來就可能給出看似不一致、實則都
    正確的答案。
    """
    report = _render(
        _make_stock_view(
            risk_flags=("DISPOSITION_STOCK",),
            risk_missing_inputs=(),
            disposition_start_date=dt.date(2026, 8, 20),
            disposition_end_date=dt.date(2026, 8, 26),
            disposition_reason="連續達注意交易標準",
        )
    )
    assert "✅ 今日公布注意：否" in report
    assert "🚨 目前處置：是" in report


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
    assert "・目前處置" not in report


# --- 模型說明 ------------------------------------------------------------------


def test_report_model_explanation_reflects_new_template():
    report = _render(_make_stock_view())
    assert "模型說明" in report
    assert "rule-v1.2.0" in report
    assert "不代表預測報酬率、上漲機率或目標價" in report
    assert "「訊號」依各因子的標準化分數區間呈現" in report
    assert "動能因子採非單調評分" in report
    assert "並非代表近期沒有上漲動能" in report
    assert "「法人籌碼」區塊顯示近 3 個交易日法人累積買超是否 > 0" in report
    assert "「技術面」區塊顯示今日收盤是否同時符合" in report
    assert "歷史分位及 T+1／T+5 統計尚未納入目前版本" in report
    # old text-v5 section is gone
    assert "「主要得分來源」" not in report
    # Absolute Signal / Relative Score separation model description
    assert "改採絕對訊號，見下" in report
    assert "「訊號」區塊中的籌碼、基本面因子，其燈號改為「絕對訊號」" in report
    assert "本次燈號語意調整不影響既有綜合分數計算公式" in report
    assert "候選池樣本過少時" in report
    assert "資料尚未到位，則明確顯示「資料尚未確認」" in report


def test_progress_checklist_shows_absolute_signal_rollout_as_done():
    report = _render(_make_stock_view())
    assert "✅ 評分模型：絕對訊號與候選池相對分數分離" in report
    assert "⬜ 評分模型：絕對訊號與候選池相對分數分離" not in report


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


def test_no_qualified_stock_report_also_shows_absolute_signal_rollout_as_done():
    """render_no_qualified_stock_report 有自己獨立的一份功能進度清單
    （見該函式定義），必須跟 render_daily_report 那份同步翻成 ✅。"""
    report = render_no_qualified_stock_report(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=5,
        strategy_version="rule-v1.2.0",
    )
    assert "✅ 評分模型：絕對訊號與候選池相對分數分離" in report
    assert "⬜ 評分模型：絕對訊號與候選池相對分數分離" not in report


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


# --- Full-size report length (10 stocks) -----------------------------------


def _make_populated_stock_view(rank: int) -> ReportStockView:
    """Every optional signal field given a real (non-None) value, plus
    two common risk flags — a realistic "complete data, ordinary day"
    stock. Deliberately does NOT include a 資料缺口 line or an
    attention/disposition reason: those are exercised separately by
    the overflow test below, since stacking them onto every one of 10
    stocks is what actually breaks the length budget (see that test's
    docstring)."""
    return _make_stock_view(
        rank=rank,
        stock_id=f"{1000 + rank}",
        stock_name="範例公司",
        close_price=Decimal("999.99"),
        change_percent=9.99,
        risk_flags=("ONE_PRICE_LIMIT_UP", "HIGH_FIVE_DAY_RETURN"),
        institutional_net_buy_3d_positive=True,
        technical_low_with_rising_signal=True,
        fundamental_growth_sustained=True,
        eps_growth_sustained=True,
        is_one_price_limit_up=True,
        volume_ratio_20d=2.4,
    )


def test_full_report_with_ten_stocks_and_populated_signals_raises_on_single_message():
    """render_daily_report() (single-string, backward-compat only)
    correctly raises once a fully-populated Top 10 no longer fits in
    one LINE message — this is the EXPECTED outcome of the
    explainable-signals rollout (every factor now carries reasons),
    not a regression. Live delivery must use
    render_daily_report_messages() instead — see the next test."""
    stocks = [_make_populated_stock_view(rank=i) for i in range(1, 11)]

    with pytest.raises(ValueError, match="exceeds LINE's"):
        render_daily_report(
            trading_date=TRADING_DATE,
            data_updated_at="16:47",
            candidate_count=42,
            eligible_count=18,
            strategy_version="rule-v1.2.0",
            ranked_stocks=stocks,
            ranking_limit=10,
        )


def test_full_report_with_ten_stocks_is_split_into_valid_messages():
    """render_daily_report_messages() is what live delivery actually
    uses — it must split the same 10-stock report into multiple
    messages, each within MAX_LINE_TEXT_UTF16_UNITS, instead of
    raising."""
    stocks = [_make_populated_stock_view(rank=i) for i in range(1, 11)]

    messages = render_daily_report_messages(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=42,
        eligible_count=18,
        strategy_version="rule-v1.2.0",
        ranked_stocks=stocks,
        ranking_limit=10,
    )

    assert len(messages) >= 2
    assert all(utf16_length(m) <= MAX_LINE_TEXT_UTF16_UNITS for m in messages)


def test_small_report_stays_single_message():
    stocks = [_make_populated_stock_view(rank=i) for i in range(1, 3)]
    messages = render_daily_report_messages(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=10,
        eligible_count=5,
        strategy_version="rule-v1.2.0",
        ranked_stocks=stocks,
        ranking_limit=10,
    )
    assert len(messages) == 1


def test_stock_block_is_never_split_across_messages():
    stocks = [_make_populated_stock_view(rank=i) for i in range(1, 11)]
    messages = render_daily_report_messages(
        trading_date=TRADING_DATE,
        data_updated_at="16:47",
        candidate_count=42,
        eligible_count=18,
        strategy_version="rule-v1.2.0",
        ranked_stocks=stocks,
        ranking_limit=10,
    )
    for stock in stocks:
        marker = f"{stock.rank}. {stock.stock_name}（{stock.stock_id}）"
        matches = [m for m in messages if marker in m]
        assert len(matches) == 1, f"{marker} should appear in exactly one message"


def test_full_report_raises_helpful_error_when_data_gaps_and_flags_compound():
    """
    Documents a real, currently-unresolved boundary: 10 stocks that
    each carry BOTH a 資料缺口 line (missing risk_quality inputs) AND
    an attention-stock reason — a plausible, not even extreme,
    combination on a volatile trading day — already exceeds
    MAX_LINE_TEXT_UTF16_UNITS on its own, well before every possible
    flag is stacked on. render_daily_report's ValueError guard is the
    intended behavior here (see its own docstring: "consider trimming
    ... or splitting into multiple messages") rather than a silent
    truncation or a crash with no explanation — this test locks in
    that the guard actually fires, with an actionable message, instead
    of the report ever being silently cut off or sent malformed.

    NOTE for a future PR: this boundary is uncomfortably close for a
    completely ordinary combination of real-world signals (an
    attention-stock reason plus one missing risk-quality input, times
    10 stocks). Trimming per-stock output (e.g. shortening the
    disposition/attention reason display, or capping missing_factor_
    names to a top-N) is worth a dedicated follow-up rather than
    silently living with more ValueError-triggered publish failures
    as real fixtures get closer to this shape.
    """
    stocks = [
        _make_stock_view(
            rank=i,
            stock_id=f"{1000 + i}",
            stock_name="範例公司",
            risk_flags=(
                "ONE_PRICE_LIMIT_UP",
                "HIGH_FIVE_DAY_RETURN",
                "ATTENTION_STOCK",
            ),
            attention_reason="近期股價及成交量異常波動",
            missing_factor_names=("risk_quality",),
            risk_missing_inputs=("is_disposition", "is_managed"),
            institutional_net_buy_3d_positive=True,
            technical_low_with_rising_signal=True,
            fundamental_growth_sustained=True,
            eps_growth_sustained=False,
        )
        for i in range(1, 11)
    ]

    try:
        render_daily_report(
            trading_date=TRADING_DATE,
            data_updated_at="16:47",
            candidate_count=42,
            eligible_count=18,
            strategy_version="rule-v1.2.0",
            ranked_stocks=stocks,
            ranking_limit=10,
        )
        assert False, "expected render_daily_report to raise ValueError"
    except ValueError as exc:
        assert "5000-UTF16-unit" in str(exc)
        assert "splitting into multiple messages" in str(exc)


# --- Step 6: Absolute Signal / Relative Score separation (rendering) --------


def test_institutional_absolute_signal_positive_ratio_is_green():
    report = _render(_make_stock_view(institutional_net_buy_ratio_5d=0.01))
    assert "🟢 籌碼：強" in report


def test_institutional_absolute_signal_zero_ratio_is_yellow():
    report = _render(_make_stock_view(institutional_net_buy_ratio_5d=0.0))
    assert "🟡 籌碼：普通" in report


def test_institutional_absolute_signal_missing_ratio_is_unknown():
    report = _render(_make_stock_view(institutional_net_buy_ratio_5d=None))
    assert "⚪ 籌碼：資料不足" in report


def test_institutional_absolute_signal_never_green_despite_top_pool_rank():
    """
    核心 regression test，直接對應 Absolute Signal / Relative Score
    分離這整個 rollout 要修的 bug：一檔法人淨賣超 -6.3% 的股票，即使
    它在候選池中排名第一（percentile 100），也絕對不能顯示 🟢——
    Absolute Signal 與 Relative Score 必須同時可見，且互不覆蓋。
    """
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 100.0,
                "fundamental": 50.0,
                "risk_quality": 90.0,
            },
            institutional_net_buy_ratio_5d=-0.063,
            relative_sample_size={"institutional": 28},
            relative_rank={"institutional": 1},
        )
    )
    assert "🟢 籌碼：強" not in report
    assert "🔴 籌碼：偏弱" in report
    assert "候選池相對分數：100/100（樣本 28）" in report


def test_fundamental_absolute_signal_boundaries():
    assert "🟢 基本面：強" in _render(_make_stock_view(revenue_yoy=0.12))
    assert "🟡 基本面：普通" in _render(_make_stock_view(revenue_yoy=0.05))
    assert "🔴 基本面：偏弱" in _render(_make_stock_view(revenue_yoy=-0.05))
    assert "⚪ 基本面：資料不足" in _render(_make_stock_view(revenue_yoy=None))


def test_fundamental_absolute_signal_never_green_despite_top_pool_rank():
    """基本面版本的核心 disagreement test：最新月營收 YoY 為 -5%
    （絕對值為負），即使候選池相對分數是滿分 100，也絕對不能顯示
    🟢 基本面：強。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 50.0,
                "fundamental": 100.0,
                "risk_quality": 90.0,
            },
            revenue_yoy=-0.05,
            relative_sample_size={"fundamental": 26},
            relative_rank={"fundamental": 1},
        )
    )
    assert "🟢 基本面：強" not in report
    assert "🔴 基本面：偏弱" in report
    assert "候選池相對分數：100/100（樣本 26）" in report


def test_liquidity_volume_price_momentum_risk_quality_rendering_is_unchanged():
    """這四個因子不在這次 Absolute Gate 的範圍內，燈號規則、行內格式
    （"｜NN/100（候選池相對／絕對規則）"）都必須維持完全不變。
    institutional/fundamental 明確設為 None（對應的原始值也是預設
    None），避免這兩個不是本測試重點的因子意外冒出候選池相對分數的
    雜訊。"""
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 45.0,
                "momentum": 70.0,
                "institutional": None,
                "fundamental": None,
                "risk_quality": 90.0,
            },
            relative_sample_size={"liquidity": 3, "volume_price": 3, "risk_quality": 3},
            relative_rank={"liquidity": 1, "volume_price": 2, "risk_quality": 1},
        )
    )
    assert "🟢 流動性：強｜80/100（候選池相對）" in report
    assert "🟡 量價：普通｜45/100（候選池相對）" in report
    assert "🟢 動能：強｜70/100（絕對規則）" in report
    assert "🟢 風險品質：強｜90/100（候選池相對）" in report
    # these four factors never show the new "候選池相對分數"/"候選池
    # 相對：第 N / M" wording, even when relative_sample_size/relative_rank
    # metadata is present for them (that metadata is harmless extra
    # data these factors simply don't render).
    assert "候選池相對分數：" not in report
    assert "候選池相對：第" not in report


def test_relative_score_normal_sample_size_shown_alongside_percentile():
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 50.0,
                "fundamental": None,
                "risk_quality": 90.0,
            },
            institutional_net_buy_ratio_5d=0.01,
            relative_sample_size={"institutional": 24},
            relative_rank={"institutional": 3},
        )
    )
    assert "候選池相對分數：50/100（樣本 24）" in report
    # scoped to the per-stock warning line's exact wording, not a bare
    # substring — the footer's general policy explanation legitimately
    # uses the phrase "樣本偏少" in prose describing when the warning
    # would apply, which must not be confused with this stock's own
    # per-stock warning actually firing.
    assert "⚠ 樣本偏少，相對結果僅供參考" not in report


def test_relative_score_warns_when_sample_size_between_five_and_nine():
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 50.0,
                "fundamental": None,
                "risk_quality": 90.0,
            },
            institutional_net_buy_ratio_5d=0.01,
            relative_sample_size={"institutional": 7},
            relative_rank={"institutional": 2},
        )
    )
    assert "候選池相對分數：50/100（樣本 7）" in report
    assert "⚠ 樣本偏少，相對結果僅供參考" in report
    assert "候選池相對：第" not in report


def test_relative_score_degrades_to_rank_when_sample_size_below_five():
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 50.0,
                "fundamental": None,
                "risk_quality": 90.0,
            },
            institutional_net_buy_ratio_5d=0.01,
            relative_sample_size={"institutional": 3},
            relative_rank={"institutional": 1},
        )
    )
    assert "候選池相對：第 1 / 3（樣本偏少）" in report
    assert "候選池相對分數：50/100" not in report


def test_relative_score_missing_metadata_does_not_crash_and_shows_plain_score():
    """既有 caller 可能仍以 relative_sample_size={}/relative_rank={}
    建構 ReportStockView（例如尚未串接 daily_ranking 的舊 caller）——
    renderer 必須用 .get(key) 安全取值，不能 KeyError，也不能捏造一個
    假的樣本數或排名。"""
    report = _render(
        _make_stock_view(
            institutional_net_buy_ratio_5d=0.01,
            revenue_yoy=None,
            relative_sample_size={},
            relative_rank={},
        )
    )
    assert "候選池相對分數：50/100" in report
    assert "（樣本" not in report
    # scoped to the per-stock warning line's exact wording — see the
    # sibling fix above for why a bare "樣本偏少" substring check would
    # false-positive against the footer's general policy explanation.
    assert "⚠ 樣本偏少，相對結果僅供參考" not in report


def test_institutional_cutoff_current_shows_confirmed_date():
    from app.domain.institutional_flow_builder import InstitutionalDataCutoff

    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 8, 6),
        confirmed_as_of_date=dt.date(2026, 8, 6),
    )
    report = _render(
        _make_stock_view(
            institutional_net_buy_ratio_5d=0.01, institutional_data_cutoff=cutoff
        )
    )
    assert "法人資料截止：T-1（2026/08/06）" in report
    # scoped to the exact per-stock cutoff line, not a bare substring
    # check — the footer's general policy explanation legitimately
    # mentions "資料尚未確認" as prose, which must not be confused with
    # this stock's own (confirmed, non-stale) cutoff line.
    assert "法人資料截止：T-1（2026/08/06）資料尚未確認" not in report


def test_institutional_cutoff_stale_shows_unconfirmed_and_forces_unknown_signal():
    """核心 T-1 stale-protection regression test：即使
    institutional_net_buy_ratio_5d 仍然有值（0.10，正值，絕對意義上
    應該是 🟢），只要 cutoff 明確表示 T-1 尚未確認，institutional
    Absolute Signal 就必須強制顯示 ⚪ 資料不足，不能信任這個舊/未確認
    的數值來畫出 🟢/🟡/🔴——這是 T-1 cutoff 這個機制存在的意義。"""
    from app.domain.institutional_flow_builder import InstitutionalDataCutoff

    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 8, 6), confirmed_as_of_date=None
    )
    report = _render(
        _make_stock_view(
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 60.0,
                "fundamental": None,
                "risk_quality": 90.0,
            },
            institutional_net_buy_ratio_5d=0.10,  # would otherwise be 🟢
            institutional_data_cutoff=cutoff,
            relative_sample_size={"institutional": 24},
            relative_rank={"institutional": 1},
        )
    )
    assert "🟢 籌碼" not in report
    assert "⚪ 籌碼：資料不足" in report
    assert "法人資料截止：T-1（2026/08/06）資料尚未確認" in report
    # the Relative Score line must also be suppressed — the whole
    # factor is being treated as unavailable for this render, not
    # merely re-labeled.
    assert "候選池相對分數：" not in report


def test_institutional_cutoff_unresolved_shows_insufficient_data():
    report = _render(
        _make_stock_view(
            institutional_net_buy_ratio_5d=0.01, institutional_data_cutoff=None
        )
    )
    assert "法人資料截止：資料不足" in report


def test_institutional_cutoff_does_not_affect_fundamental_factor():
    """T-1 stale-protection 只作用於 institutional 因子，不應該波及
    fundamental（兩者是完全獨立的資料來源與截止日概念）。"""
    from app.domain.institutional_flow_builder import InstitutionalDataCutoff

    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 8, 6), confirmed_as_of_date=None
    )
    report = _render(
        _make_stock_view(
            institutional_net_buy_ratio_5d=0.10,
            institutional_data_cutoff=cutoff,
            revenue_yoy=0.12,
        )
    )
    assert "⚪ 籌碼：資料不足" in report
    assert "🟢 基本面：強" in report
