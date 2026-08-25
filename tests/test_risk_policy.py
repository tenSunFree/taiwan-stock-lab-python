import pytest

from app.domain.risk_policy import RiskPolicy, RiskPolicyConfig, build_risk_quality_raw


def make_policy(**overrides):
    config = RiskPolicyConfig(**overrides)
    return RiskPolicy(config)


def test_disposition_stock_allowed_but_flagged_by_default():
    """
    v1 of the official 注意/處置 rollout is display-only: a disposition
    stock must still reach the report with a DISPOSITION_STOCK flag,
    not silently vanish from the candidate pool. This is a much more
    serious official signal than an attention stock, but the decision
    to actually exclude on it is deferred until backtested — see
    RiskPolicyConfig.allow_disposition_stock's own docstring.
    """
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=True,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is False
    assert "DISPOSITION_STOCK" in result.risk_flags


def test_disposition_stock_excluded_when_policy_disallows():
    policy = make_policy(allow_disposition_stock=False)
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=True,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is True
    assert "disposition stock" in result.exclusion_reason


def test_managed_stock_allowed_but_flagged_by_default():
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=True,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is False
    assert "MANAGED_STOCK" in result.risk_flags


def test_managed_stock_excluded_when_policy_disallows():
    policy = make_policy(allow_managed_stock=False)
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=True,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is True
    assert "full-cash-delivery" in result.exclusion_reason


def test_attention_stock_allowed_but_flagged_by_default():
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=True,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is False
    assert "ATTENTION_STOCK" in result.risk_flags


def test_attention_stock_excluded_when_policy_disallows():
    policy = make_policy(allow_attention_stock=False)
    result = policy.assess(
        stock_id="1234",
        is_attention=True,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is True


def test_one_price_limit_up_excluded_when_policy_disallows():
    """
    Regression test: RiskPolicyConfig.allow_one_price_limit_up existed
    but assess() never actually checked it — this confirms the fix.
    """
    policy = make_policy(allow_one_price_limit_up=False)
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=True,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is True


def test_one_price_limit_up_allowed_by_default_still_flagged():
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=True,
        consecutive_limit_up_days=1,
        return_5d=0.1,
    )
    assert result.is_excluded is False
    assert "ONE_PRICE_LIMIT_UP" in result.risk_flags


def test_excessive_consecutive_limit_up_flagged():
    policy = make_policy(maximum_consecutive_limit_up_days=3)
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=5,
        return_5d=0.1,
    )
    assert "EXCESSIVE_CONSECUTIVE_LIMIT_UP" in result.risk_flags


def test_high_five_day_return_flagged():
    policy = make_policy(excessive_return_5d=0.35)
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.5,
    )
    assert "HIGH_FIVE_DAY_RETURN" in result.risk_flags


def test_clean_stock_has_no_flags():
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.05,
    )
    assert result.is_excluded is False
    assert result.risk_flags == tuple()
    assert result.missing_inputs == tuple()


def test_unknown_risk_statuses_are_reported_as_missing():
    """
    None (unknown) must never be silently treated as False (confirmed
    clean) — it must show up in missing_inputs so downstream code
    (build_risk_quality_raw) can refuse to score it.
    """
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=None,
        is_disposition=None,
        is_managed=None,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=None,
        return_5d=0.1,
    )
    assert result.is_excluded is False
    assert "is_attention" in result.missing_inputs
    assert "is_disposition" in result.missing_inputs
    assert "is_managed" in result.missing_inputs
    assert "consecutive_limit_up_days" in result.missing_inputs


def test_unknown_disposition_is_not_treated_as_excluded():
    """None must not accidentally satisfy `is is True` exclusion checks."""
    policy = make_policy()
    result = policy.assess(
        stock_id="1234",
        is_attention=None,
        is_disposition=None,
        is_managed=None,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=None,
        return_5d=0.1,
    )
    assert result.is_excluded is False


# --- build_risk_quality_raw ---


def test_build_risk_quality_raw_complete_inputs_no_flags_is_one():
    policy = make_policy()
    assessment = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.05,
    )
    assert build_risk_quality_raw(assessment) == pytest.approx(1.0)


def test_build_risk_quality_raw_complete_inputs_with_flags():
    policy = make_policy()
    assessment = policy.assess(
        stock_id="1234",
        is_attention=False,
        is_disposition=False,
        is_managed=False,
        is_ky=True,
        is_one_price_limit_up=True,
        consecutive_limit_up_days=1,
        return_5d=0.05,
    )
    # KY_STOCK (0.10) + ONE_PRICE_LIMIT_UP (0.20)
    assert build_risk_quality_raw(assessment) == pytest.approx(0.70)


def test_build_risk_quality_raw_missing_inputs_returns_none_not_one():
    """
    The core regression this Step exists to fix: unknown inputs must
    yield None, never a fabricated 1.0 that looks like "confirmed
    clean."
    """
    policy = make_policy()
    assessment = policy.assess(
        stock_id="1234",
        is_attention=None,
        is_disposition=None,
        is_managed=None,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=None,
        return_5d=0.05,
    )
    assert assessment.risk_flags == tuple()  # no flags raised...
    assert build_risk_quality_raw(assessment) is None  # ...but NOT scored as clean


def test_build_risk_quality_raw_clamped_to_zero():
    """
    Uses an explicit custom penalties table (not the real
    RISK_FLAG_PENALTIES default) so this test verifies the clamping
    ARITHMETIC in isolation, independent of whatever the real-world
    per-flag weights happen to be tuned to at any given time — see
    RISK_FLAG_PENALTIES's own docstring for why ATTENTION_STOCK /
    DISPOSITION_STOCK / MANAGED_STOCK are deliberately 0.0 there for
    now, which would otherwise make this test unable to reach 1.0
    total penalty using only real default weights.
    """
    policy = make_policy(maximum_consecutive_limit_up_days=3, excessive_return_5d=0.35)
    assessment = policy.assess(
        stock_id="1234",
        is_attention=True,
        is_disposition=False,
        is_managed=False,
        is_ky=True,
        is_one_price_limit_up=True,
        consecutive_limit_up_days=10,
        return_5d=0.5,
    )
    custom_penalties = {
        "ATTENTION_STOCK": 0.15,
        "KY_STOCK": 0.10,
        "ONE_PRICE_LIMIT_UP": 0.20,
        "EXCESSIVE_CONSECUTIVE_LIMIT_UP": 0.30,
        "HIGH_FIVE_DAY_RETURN": 0.25,
    }
    # 0.15 + 0.10 + 0.20 + 0.30 + 0.25 = 1.00 -> clamped, not negative
    assert build_risk_quality_raw(assessment, penalties=custom_penalties) == 0.0


def test_build_risk_quality_raw_unknown_flag_no_penalty():
    from app.domain.risk_policy import RiskAssessment

    assessment = RiskAssessment(
        stock_id="1234",
        is_excluded=False,
        exclusion_reason=None,
        risk_flags=("SOME_FUTURE_FLAG",),
        missing_inputs=(),
    )
    assert build_risk_quality_raw(assessment) == pytest.approx(1.0)


def test_attention_disposition_managed_flags_do_not_penalize_score_in_v1():
    """
    Regression test for the v1 "官方風控" rollout requirement: display
    the official attention/disposition/managed status honestly, but do
    NOT let it affect the score yet — that decision is deferred until
    backtested (see RISK_FLAG_PENALTIES's own docstring). Uses the
    REAL default RISK_FLAG_PENALTIES (no override), unlike the
    clamping test above, specifically to catch a regression where
    someone bumps one of these three back above 0.0 by accident.
    """
    policy = make_policy()
    assessment = policy.assess(
        stock_id="1234",
        is_attention=True,
        is_disposition=True,
        is_managed=True,
        is_ky=False,
        is_one_price_limit_up=False,
        consecutive_limit_up_days=1,
        return_5d=0.05,
    )
    assert set(assessment.risk_flags) == {
        "ATTENTION_STOCK",
        "DISPOSITION_STOCK",
        "MANAGED_STOCK",
    }
    assert build_risk_quality_raw(assessment) == pytest.approx(1.0)
