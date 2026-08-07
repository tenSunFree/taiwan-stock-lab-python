from app.domain.risk_policy import RiskPolicy, RiskPolicyConfig


def make_policy(**overrides):
    config = RiskPolicyConfig(**overrides)
    return RiskPolicy(config)


def test_disposition_stock_is_hard_excluded():
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
    assert result.is_excluded is True
    assert "disposition stock" in result.exclusion_reason


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
