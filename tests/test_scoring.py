from app.domain.features import StockFeatures
from app.domain.scoring import FACTOR_WEIGHTS, score_candidates, select_top_five


def make_features(stock_id, turnover=100_000_000, vol_ratio=1.5, ret5=0.05, inst=0.02, rev=0.10, risk=0.9):
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
    assert scored["A"].factor_scores["liquidity"] > scored["B"].factor_scores["liquidity"]


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


def test_select_top_five_excludes_low_completeness():
    features = [make_features(f"S{i}") for i in range(8)]
    scored = score_candidates(features)

    # manually push one stock's completeness down to simulate severely incomplete data
    low_completeness = scored[0].__class__(
        stock_id="LOWQ",
        total_score=99.0,  # score is high, but data completeness is too low to enter the Top 5
        data_completeness=0.5,
        factor_scores={},
        risk_flags=tuple(),
    )
    scored_with_low = scored + [low_completeness]

    turnover_map = {s.stock_id: 100_000_000.0 for s in features}
    turnover_map["LOWQ"] = 999_999_999.0

    top5 = select_top_five(scored_with_low, turnover_map, minimum_data_completeness=0.80)

    assert "LOWQ" not in [s.stock_id for s in top5]
    assert len(top5) == 5


def test_select_top_five_returns_at_most_five():
    features = [make_features(f"S{i}", turnover=100_000_000 + i * 1000) for i in range(10)]
    scored = score_candidates(features)
    turnover_map = {f.stock_id: f.turnover for f in features}
    top5 = select_top_five(scored, turnover_map)
    assert len(top5) == 5
