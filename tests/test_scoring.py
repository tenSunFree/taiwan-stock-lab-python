import pytest

from app.domain.features import StockFeatures
from app.domain.scoring import (
    FACTOR_WEIGHTS,
    RELATIVE_SCORE_FACTORS,
    ScoredStock,
    score_candidates,
    select_top_n,
)


def make_features(
    stock_id,
    turnover=100_000_000,
    vol_ratio=1.5,
    ret5=0.05,
    inst=0.02,
    rev=0.10,
    risk=0.9,
):
    return StockFeatures(
        stock_id=stock_id,
        turnover=turnover,
        average_turnover_20d=turnover * 0.8,
        volume_ratio_20d=vol_ratio,
        return_5d=ret5,
        return_20d=None,
        institutional_net_buy_ratio_5d=inst,
        revenue_yoy=rev,
        risk_quality_raw=risk,
    )


def test_weights_sum_to_one():
    assert abs(sum(FACTOR_WEIGHTS.values()) - 1.0) < 1e-9


def test_higher_turnover_gets_higher_liquidity_score():
    features = [
        make_features("A", turnover=500_000_000),
        make_features("B", turnover=50_000_000),
        make_features("C", turnover=100_000_000),
    ]
    scored = {s.stock_id: s for s in score_candidates(features)}
    assert (
        scored["A"].factor_scores["liquidity"] > scored["B"].factor_scores["liquidity"]
    )


def test_missing_factor_reduces_completeness_not_filled_as_50():
    features = [
        make_features("A"),
        StockFeatures(
            stock_id="B",
            turnover=100_000_000,
            average_turnover_20d=None,
            volume_ratio_20d=None,  # missing volume/price factor
            return_5d=0.05,
            return_20d=None,
            institutional_net_buy_ratio_5d=None,  # missing institutional factor
            revenue_yoy=0.1,
            risk_quality_raw=0.9,
        ),
    ]
    scored = {s.stock_id: s for s in score_candidates(features)}
    assert scored["B"].data_completeness < 1.0
    assert scored["B"].factor_scores["volume_price"] is None
    assert scored["B"].factor_scores["institutional"] is None


def test_select_top_n_excludes_low_completeness():
    features = [make_features(f"S{i}") for i in range(12)]
    scored = score_candidates(features)

    # manually push one stock's completeness down to simulate severely incomplete data
    low_completeness = scored[
        0
    ].__class__(
        stock_id="LOWQ",
        total_score=99.0,  # score is high, but data completeness is too low to enter the ranking
        data_completeness=0.5,
        factor_scores={},
        risk_flags=tuple(),
    )
    scored_with_low = scored + [low_completeness]

    turnover_map = {s.stock_id: 100_000_000.0 for s in features}
    turnover_map["LOWQ"] = 999_999_999.0

    top10 = select_top_n(
        scored_with_low, turnover_map, limit=10, minimum_data_completeness=0.80
    )

    assert "LOWQ" not in [s.stock_id for s in top10]
    assert len(top10) == 10


def test_select_top_n_returns_at_most_requested_limit():
    features = [
        make_features(f"S{i}", turnover=100_000_000 + i * 1000) for i in range(15)
    ]
    scored = score_candidates(features)
    turnover_map = {f.stock_id: f.turnover for f in features}
    top10 = select_top_n(scored, turnover_map, limit=10)
    assert len(top10) == 10


def test_select_top_n_defaults_to_limit_ten():
    features = [
        make_features(f"S{i}", turnover=100_000_000 + i * 1000) for i in range(15)
    ]
    scored = score_candidates(features)
    turnover_map = {f.stock_id: f.turnover for f in features}
    top_default = select_top_n(scored, turnover_map)
    assert len(top_default) == 10


def test_select_top_n_respects_a_smaller_custom_limit():
    features = [
        make_features(f"S{i}", turnover=100_000_000 + i * 1000) for i in range(15)
    ]
    scored = score_candidates(features)
    turnover_map = {f.stock_id: f.turnover for f in features}
    top3 = select_top_n(scored, turnover_map, limit=3)
    assert len(top3) == 3


def test_select_top_n_rejects_non_positive_limit():
    features = [make_features("A")]
    scored = score_candidates(features)
    turnover_map = {f.stock_id: f.turnover for f in features}
    with pytest.raises(ValueError):
        select_top_n(scored, turnover_map, limit=0)


# --- Step 3: real-world completeness state (risk_quality_raw always None) ---


def test_missing_risk_quality_alone_still_meets_default_completeness_threshold():
    """
    Regression test tied to the current real-world state: risk_quality_raw
    is None for every stock (RiskPolicy's attention/disposition/managed
    inputs have no wired-in data source yet). This must NOT, by itself,
    push a stock below the default 0.80 completeness threshold —
    available_weight = 1.00 - 0.10(risk_quality) = 0.90 >= 0.80.
    """
    features = [
        StockFeatures(
            stock_id="A",
            turnover=100_000_000,
            average_turnover_20d=80_000_000,
            volume_ratio_20d=1.5,
            return_5d=0.05,
            return_20d=None,
            institutional_net_buy_ratio_5d=0.02,
            revenue_yoy=0.10,
            risk_quality_raw=None,  # the current real-world state
        )
    ]
    scored = score_candidates(features)
    assert len(scored) == 1
    assert scored[0].data_completeness == pytest.approx(0.90)
    assert scored[0].data_completeness >= 0.80
    assert scored[0].factor_scores["risk_quality"] is None


def test_missing_risk_quality_plus_institutional_falls_below_threshold():
    """
    The flip side of the test above: missing risk_quality (0.10) PLUS
    institutional (0.15) leaves only 0.75 available weight, which must
    fail the default 0.80 ranking completeness gate. Without this test,
    a broken available_weight calculation could silently let every
    stock through regardless of how much data is actually missing.
    """
    features = [
        StockFeatures(
            stock_id="A",
            turnover=100_000_000,
            average_turnover_20d=80_000_000,
            volume_ratio_20d=1.5,
            return_5d=0.05,
            return_20d=None,
            institutional_net_buy_ratio_5d=None,
            revenue_yoy=0.10,
            risk_quality_raw=None,
        )
    ]
    scored = score_candidates(features)
    assert len(scored) == 1
    assert scored[0].data_completeness == pytest.approx(0.75)

    top_ranked = select_top_n(
        scored, {"A": 100_000_000.0}, minimum_data_completeness=0.80
    )
    assert top_ranked == []


def test_select_top_n_uses_turnover_as_tie_breaker():
    scored = [
        ScoredStock(
            stock_id="LOW_TURNOVER",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=(),
        ),
        ScoredStock(
            stock_id="HIGH_TURNOVER",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=(),
        ),
    ]

    top_ranked = select_top_n(
        scored,
        {"LOW_TURNOVER": 100_000_000.0, "HIGH_TURNOVER": 500_000_000.0},
    )

    assert [stock.stock_id for stock in top_ranked] == ["HIGH_TURNOVER", "LOW_TURNOVER"]


# --- Step 4: relative_sample_size / relative_rank are additive-only ---------


def test_relative_sample_size_and_rank_are_populated_for_relative_factors():
    features = [
        make_features(f"S{i}", inst=0.01 * i, rev=0.01 * i) for i in range(1, 4)
    ]

    scored = score_candidates(features)

    for stock in scored:
        for factor_name in RELATIVE_SCORE_FACTORS:
            assert stock.relative_sample_size[factor_name] == 3
            assert stock.relative_rank[factor_name] in (1, 2, 3)
        # momentum is never a key in either dict (absolute rule, not a
        # pool percentile — see RELATIVE_SCORE_FACTORS's own docstring)
        assert "momentum" not in stock.relative_sample_size
        assert "momentum" not in stock.relative_rank


def test_relative_rank_is_none_when_stocks_own_value_is_missing():
    features = [
        make_features("A"),
        make_features("B"),
        StockFeatures(
            stock_id="C",
            turnover=100_000_000,
            average_turnover_20d=80_000_000,
            volume_ratio_20d=1.5,
            return_5d=0.05,
            return_20d=None,
            institutional_net_buy_ratio_5d=None,  # missing
            revenue_yoy=0.10,
            risk_quality_raw=0.9,
        ),
    ]
    scored = {s.stock_id: s for s in score_candidates(features)}
    assert scored["C"].relative_rank["institutional"] is None
    # sample size still reflects the other 2 stocks that DID have a
    # value, not 0 and not 3
    assert scored["C"].relative_sample_size["institutional"] == 2
    assert scored["A"].relative_sample_size["institutional"] == 2


def test_total_score_formula_is_unchanged_by_relative_score_metadata():
    """
    Regression test: adding relative_sample_size/relative_rank must
    NOT change total_score/data_completeness/factor_scores in any way.
    Verified by independently recomputing the pre-existing
    weighted-average formula from factor_scores and comparing against
    total_score.
    """
    features = [make_features(f"S{i}") for i in range(5)]
    scored = score_candidates(features)

    for stock in scored:
        weighted_sum = 0.0
        available_weight = 0.0
        for factor_name, weight in FACTOR_WEIGHTS.items():
            value = stock.factor_scores.get(factor_name)
            if value is None:
                continue
            weighted_sum += value * weight
            available_weight += weight
        expected_total_score = round(weighted_sum / available_weight, 2)
        assert stock.total_score == pytest.approx(expected_total_score)
