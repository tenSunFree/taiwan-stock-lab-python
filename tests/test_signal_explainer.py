from app.reports.signal_explainer import (
    explain_fundamental,
    explain_institutional,
    explain_liquidity,
    explain_momentum,
    explain_risk_quality,
    explain_volume_price,
)


def test_liquidity_reason_uses_turnover_not_ratio():
    result = explain_liquidity(turnover=5.43e8, average_turnover_20d=2.35e8, score=82.0)
    assert any("成交金額" in r for r in result.reasons)
    # 均量倍數必須在 supplemental，不在 reasons
    assert not any("倍" in r for r in result.reasons)
    assert any("倍" in s for s in result.supplemental)


def test_liquidity_missing_score_is_data_insufficient():
    result = explain_liquidity(turnover=None, average_turnover_20d=None, score=None)
    assert result.data_status == "資料不足"


def test_liquidity_missing_average_is_partial_but_still_scored():
    result = explain_liquidity(turnover=5.43e8, average_turnover_20d=None, score=82.0)
    assert result.data_status == "部分缺失"
    assert any("成交金額" in r for r in result.reasons)
    assert result.supplemental == ()


def test_volume_price_missing_is_data_insufficient():
    result = explain_volume_price(volume_ratio_20d=None, score=None)
    assert result.data_status == "資料不足"


def test_volume_ratio_below_one_explains_weak_volume():
    result = explain_volume_price(volume_ratio_20d=0.82, score=28.0)
    assert any("0.82" in r for r in result.reasons)
    assert any("低於近 20 日平均量" in r for r in result.reasons)


def test_fundamental_never_mentions_eps():
    result = explain_fundamental(revenue_yoy=0.246, score=85.0)
    assert not any("EPS" in r for r in result.reasons)
    assert any("+24.6%" in r for r in result.reasons)


def test_fundamental_missing_is_data_insufficient():
    result = explain_fundamental(revenue_yoy=None, score=None)
    assert result.data_status == "資料不足"


def test_momentum_overheated_uses_overheated_wording():
    result = explain_momentum(
        return_5d=0.42,
        return_20d=0.50,
        score=20.0,
        risk_flags=("HIGH_FIVE_DAY_RETURN",),
    )
    assert any("過熱" in r for r in result.reasons)


def test_momentum_ideal_band():
    result = explain_momentum(
        return_5d=0.08,
        return_20d=0.10,
        score=100.0,
        risk_flags=(),
    )
    assert any("理想動能區間" in r for r in result.reasons)


def test_momentum_high_return_without_overheat_flag_is_not_mislabeled_overheated():
    """excessive_return_5d 跟 bounded_momentum_score 的門檻可以各自設定，
    分數還在偏弱以上、旗標也沒觸發時，不該顯示「過熱」字樣。"""
    result = explain_momentum(
        return_5d=0.20,
        return_20d=0.25,
        score=70.0,
        risk_flags=(),
    )
    assert not any("過熱" in r for r in result.reasons)


def test_momentum_missing_is_data_insufficient():
    result = explain_momentum(
        return_5d=None, return_20d=None, score=None, risk_flags=()
    )
    assert result.data_status == "資料不足"


def test_institutional_positive_but_small():
    result = explain_institutional(institutional_net_buy_ratio_5d=0.013, score=54.0)
    assert any("+1.3%" in r for r in result.reasons)
    assert any("力度有限" in r for r in result.reasons)


def test_institutional_negative_is_net_sell():
    result = explain_institutional(institutional_net_buy_ratio_5d=-0.02, score=15.0)
    assert any("淨賣超" in r for r in result.reasons)


def test_risk_quality_zero_penalty_flag_goes_to_supplemental():
    result = explain_risk_quality(
        risk_quality_raw=0.80,
        score=55.0,
        risk_flags=("ATTENTION_STOCK", "ONE_PRICE_LIMIT_UP"),
        risk_missing_inputs=(),
    )
    assert any("一字漲停" in r and "扣" in r for r in result.reasons)
    assert any("注意股" in s and "未影響" in s for s in result.supplemental)
    assert not any("注意股" in r for r in result.reasons)


def test_risk_quality_no_flags_at_all():
    result = explain_risk_quality(
        risk_quality_raw=1.0,
        score=90.0,
        risk_flags=(),
        risk_missing_inputs=(),
    )
    assert any("1.00" in r for r in result.reasons)
    assert result.supplemental == ()
    assert result.data_status == "完整"


def test_risk_quality_missing_inputs_lists_confirmed_and_missing():
    result = explain_risk_quality(
        risk_quality_raw=None,
        score=None,
        risk_flags=(),
        risk_missing_inputs=("is_disposition", "consecutive_limit_up_days"),
    )
    assert any("注意股" in c for c in result.confirmed)
    assert any("處置" in m for m in result.missing)
    assert result.data_status == "部分缺失"


def test_risk_quality_all_inputs_missing_is_data_insufficient():
    result = explain_risk_quality(
        risk_quality_raw=None,
        score=None,
        risk_flags=(),
        risk_missing_inputs=(
            "is_attention",
            "is_disposition",
            "is_managed",
            "consecutive_limit_up_days",
        ),
    )
    assert result.confirmed == ()
    assert result.data_status == "資料不足"
