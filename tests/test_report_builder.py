import datetime as dt
from dataclasses import replace
from decimal import Decimal

import pytest

from app.domain.candidate_builder import Candidate
from app.domain.features import StockFeatures
from app.domain.institutional_flow_builder import InstitutionalDataCutoff
from app.domain.limit_up import LimitUpResult, LimitUpSource
from app.domain.models import DailyPrice, Market, SecurityType, StockMaster
from app.domain.scoring import FACTOR_WEIGHTS, ScoredStock
from app.reports.report_builder import build_report_stocks
from app.reports.text_renderer import render_daily_report

TRADING_DATE = dt.date(2026, 8, 7)


def _make_stock(
    stock_id: str = "1234", stock_name: str = "Example Corp A"
) -> StockMaster:
    return StockMaster(
        stock_id=stock_id,
        stock_name=stock_name,
        market=Market.TWSE,
        security_type=SecurityType.COMMON_STOCK,
    )


def _make_candidate(
    *,
    stock_id: str = "1234",
    stock_name: str = "Example Corp A",
    open_price: str = "100",
    high_price: str = "110",
    low_price: str = "100",
    close_price: str = "110",
    reference_price: str = "100",
    limit_up_price: str = "110",
) -> Candidate:
    stock = _make_stock(stock_id, stock_name)
    price = DailyPrice(
        trading_date=TRADING_DATE,
        stock_id=stock_id,
        reference_price=Decimal(reference_price),
        open_price=Decimal(open_price),
        high_price=Decimal(high_price),
        low_price=Decimal(low_price),
        close_price=Decimal(close_price),
        volume=1_000_000,
        turnover=Decimal("100000000"),
    )
    limit_up = LimitUpResult(
        is_close_limit_up=True,
        has_touched_limit_up=True,
        limit_up_price=Decimal(limit_up_price),
        limit_up_source=LimitUpSource.CALCULATED,
        reason="test fixture",
    )
    return Candidate(stock=stock, price=price, limit_up=limit_up)


def _make_features(stock_id: str, **overrides) -> StockFeatures:
    """Minimal StockFeatures fixture for tests that don't care about
    the specific enrichment values — build_report_stocks() as of
    text-v6 requires a matching entry in features_by_stock for every
    ranked stock_id (see that function's fail-fast docstring), so
    every test that calls it needs at least this much."""
    defaults = dict(
        stock_id=stock_id,
        turnover=1_000_000.0,
        average_turnover_20d=800_000.0,
        volume_ratio_20d=1.5,
        return_5d=0.05,
        return_20d=0.10,
        institutional_net_buy_ratio_5d=0.01,
        revenue_yoy=0.12,
        risk_quality_raw=None,
        risk_flags=(),
        risk_missing_inputs=(),
    )
    defaults.update(overrides)
    return StockFeatures(**defaults)


def test_build_report_stocks_maps_names_and_rank():
    scored = [
        ScoredStock(
            stock_id="1234",
            total_score=84.2,
            data_completeness=0.96,
            factor_scores={"liquidity": 90.0, "momentum": 40.0, "fundamental": 85.0},
            risk_flags=("HIGH_FIVE_DAY_RETURN",),
        ),
        ScoredStock(
            stock_id="5678",
            total_score=80.4,
            data_completeness=0.91,
            factor_scores={"institutional": 75.0, "liquidity": 70.0},
            risk_flags=(),
        ),
    ]
    stock_master = {
        "1234": _make_stock("1234", "Example Corp A"),
        "5678": _make_stock("5678", "Example Corp B"),
    }
    candidates = {
        "1234": _make_candidate(stock_id="1234", stock_name="Example Corp A"),
        "5678": _make_candidate(stock_id="5678", stock_name="Example Corp B"),
    }
    features_by_stock = {
        "1234": _make_features("1234"),
        "5678": _make_features("5678"),
    }

    views = build_report_stocks(
        ranked_stocks=scored,
        stock_master=stock_master,
        candidates=candidates,
        features_by_stock=features_by_stock,
    )

    assert [v.rank for v in views] == [1, 2]
    assert views[0].stock_name == "Example Corp A"
    assert views[1].stock_name == "Example Corp B"
    assert views[0].top_factor_names == ("流動性", "基本面")


def test_build_report_stocks_falls_back_to_stock_id_when_name_missing():
    scored = [
        ScoredStock(
            stock_id="9999",
            total_score=70.0,
            data_completeness=1.0,
            factor_scores={},
            risk_flags=(),
        )
    ]
    candidates = {"9999": _make_candidate(stock_id="9999")}
    features_by_stock = {"9999": _make_features("9999")}
    views = build_report_stocks(
        ranked_stocks=scored,
        stock_master={},
        candidates=candidates,
        features_by_stock=features_by_stock,
    )
    assert views[0].stock_name == "9999"


def test_build_report_stocks_computes_close_price_and_change_percent():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={
                "liquidity": 90.0,
                "volume_price": 80.0,
                "momentum": 75.0,
                "institutional": 70.0,
                "fundamental": 85.0,
                "risk_quality": None,
            },
            risk_flags=(),
        )
    ]
    candidate = _make_candidate(
        stock_id="1101",
        open_price="41.00",
        high_price="44.65",
        low_price="40.80",
        close_price="44.65",
        reference_price="40.60",
        limit_up_price="44.65",
    )

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].close_price == Decimal("44.65")
    assert result[0].change_percent == pytest.approx(9.975369458128078)
    # open (41.00) != close (44.65), so this is NOT a one-price limit-up
    assert result[0].is_one_price_limit_up is False
    assert result[0].missing_factor_names == ("risk_quality",)


def test_build_report_stocks_detects_one_price_limit_up():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={"liquidity": 90.0, "risk_quality": None},
            risk_flags=("ONE_PRICE_LIMIT_UP",),
        )
    ]
    # open == high == low == close == limit_up_price
    candidate = _make_candidate(
        stock_id="1101",
        open_price="110",
        high_price="110",
        low_price="110",
        close_price="110",
        reference_price="100",
        limit_up_price="110",
    )

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].is_one_price_limit_up is True


def test_build_report_stocks_leaves_change_percent_none_without_reference_price():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=(),
        )
    ]
    candidate = _make_candidate(stock_id="1101")
    candidate = replace(candidate, price=replace(candidate.price, reference_price=None))

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].change_percent is None


def test_build_report_stocks_raises_on_missing_candidate():
    stock = _make_stock("1101", "測試水泥")
    scored = ScoredStock(
        stock_id="1101",
        total_score=80.0,
        data_completeness=0.90,
        factor_scores={},
        risk_flags=(),
    )

    with pytest.raises(RuntimeError, match="does not exist in CandidateBuilder output"):
        build_report_stocks(
            ranked_stocks=[scored],
            stock_master={"1101": stock},
            candidates={},
            # candidates check fires before features_by_stock is ever
            # looked at, so an empty dict here is safe and deliberate
            # — this test is specifically about the `candidates` guard.
            features_by_stock={},
        )


def test_build_report_stocks_raises_on_missing_features():
    """Mirror of test_build_report_stocks_raises_on_missing_candidate,
    for the features_by_stock invariant added in text-v6 — a ranked
    stock without a matching StockFeatures entry means score_candidates
    was called with a features list inconsistent with `candidates`,
    which is a pipeline bug, not ordinary missing data."""
    stock = _make_stock("1101", "測試水泥")
    candidate = _make_candidate(stock_id="1101")
    scored = ScoredStock(
        stock_id="1101",
        total_score=80.0,
        data_completeness=0.90,
        factor_scores={},
        risk_flags=(),
    )

    with pytest.raises(RuntimeError, match="does not exist in features_by_stock"):
        build_report_stocks(
            ranked_stocks=[scored],
            stock_master={"1101": stock},
            candidates={"1101": candidate},
            features_by_stock={},
        )


# --- regulatory_by_stock merge (Step 6) --------------------------------------


def test_build_report_stocks_populates_disposition_detail_from_regulatory_by_stock():
    from app.domain.models import RegulatoryRiskStatus

    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=("DISPOSITION_STOCK",),
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
        regulatory_by_stock={
            "1101": RegulatoryRiskStatus(
                trading_date=TRADING_DATE,
                stock_id="1101",
                is_disposition=True,
                disposition_start_date=dt.date(2026, 8, 24),
                disposition_end_date=dt.date(2026, 8, 28),
                disposition_reason="連續三次",
            )
        },
    )

    assert result[0].disposition_start_date == dt.date(2026, 8, 24)
    assert result[0].disposition_end_date == dt.date(2026, 8, 28)
    assert result[0].disposition_reason == "連續三次"


def test_build_report_stocks_leaves_regulatory_fields_none_when_not_flagged():
    """The ordinary case: a ranked stock that simply isn't under
    attention/disposition at all — absent from regulatory_by_stock is
    NOT a pipeline invariant violation, unlike a missing `candidates`
    entry."""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=(),
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
        regulatory_by_stock={},
    )

    assert result[0].attention_reason is None
    assert result[0].disposition_start_date is None
    assert result[0].disposition_reason is None


def test_build_report_stocks_defaults_regulatory_by_stock_to_empty():
    """regulatory_by_stock is optional — callers that don't pass it at
    all (e.g. existing tests written before Step 6) must not break."""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={},
            risk_flags=(),
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].attention_reason is None


# --- StockFeatures merge (text-v6) --------------------------------------------


def test_build_report_stocks_populates_volume_ratio_and_factor_scores_from_features():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            data_completeness=0.90,
            factor_scores={"liquidity": 90.0, "risk_quality": None},
            risk_flags=(),
            risk_missing_inputs=("is_managed", "consecutive_limit_up_days"),
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101", volume_ratio_20d=2.4)},
    )

    assert result[0].volume_ratio_20d == 2.4
    assert result[0].factor_scores == {"liquidity": 90.0, "risk_quality": None}
    assert result[0].risk_missing_inputs == (
        "is_managed",
        "consecutive_limit_up_days",
    )


def test_build_report_stocks_carries_institutional_net_buy_3d_positive():
    """text-v8：features_by_stock 的 institutional_net_buy_3d_positive
    必須原封不動地帶到 ReportStockView，供「法人籌碼」區塊使用——這是
    獨立於 factor_scores 裡 institutional 評分因子之外的顯示訊號，見
    app.domain.institutional_flow_builder 的模組說明。"""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={
            "1101": _make_features("1101", institutional_net_buy_3d_positive=True)
        },
    )

    assert result[0].institutional_net_buy_3d_positive is True


def test_build_report_stocks_institutional_net_buy_3d_positive_defaults_to_none():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].institutional_net_buy_3d_positive is None


def test_build_report_stocks_carries_technical_low_with_rising_signal():
    """text-v9：features_by_stock 的 technical_low_with_rising_signal
    必須原封不動地帶到 ReportStockView，供「技術面」區塊使用——這是
    獨立於 factor_scores 裡 momentum 評分因子之外的顯示訊號，見
    app.domain.technical_signal_builder 的模組說明。"""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={
            "1101": _make_features("1101", technical_low_with_rising_signal=True)
        },
    )

    assert result[0].technical_low_with_rising_signal is True


def test_build_report_stocks_technical_low_with_rising_signal_defaults_to_none():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].technical_low_with_rising_signal is None


def test_build_report_stocks_carries_fundamental_growth_sustained():
    """text-v10：features_by_stock 的 fundamental_growth_sustained
    必須原封不動地帶到 ReportStockView，供「基本面」區塊使用——這是
    獨立於 factor_scores 裡 fundamental 評分因子之外的顯示訊號，見
    app.domain.monthly_revenue_builder 的模組說明。"""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={
            "1101": _make_features("1101", fundamental_growth_sustained=True)
        },
    )

    assert result[0].fundamental_growth_sustained is True


def test_build_report_stocks_fundamental_growth_sustained_defaults_to_none():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].fundamental_growth_sustained is None


def test_build_report_stocks_carries_eps_growth_sustained():
    """text-v11：features_by_stock 的 eps_growth_sustained 必須原封不動
    地帶到 ReportStockView，跟 fundamental_growth_sustained（營收）分開
    傳遞，不在這一層合併——合併是 text_renderer 在渲染時才做的事，見
    app.domain.eps_growth_builder.combine_fundamental_growth_signal。"""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={
            "1101": _make_features(
                "1101", fundamental_growth_sustained=False, eps_growth_sustained=True
            )
        },
    )

    # Both components pass through independently — eps_growth_sustained
    # being True must not silently overwrite fundamental_growth_sustained's
    # own False value or vice versa.
    assert result[0].eps_growth_sustained is True
    assert result[0].fundamental_growth_sustained is False


def test_build_report_stocks_eps_growth_sustained_defaults_to_none():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].eps_growth_sustained is None


# --- Step 5: Absolute Signal / Relative Score fields pass through unchanged --


def test_build_report_stocks_carries_institutional_data_cutoff_through():
    from app.domain.institutional_flow_builder import InstitutionalDataCutoff

    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 8, 6),
        confirmed_as_of_date=dt.date(2026, 8, 6),
    )
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={
            "1101": _make_features("1101", institutional_data_cutoff=cutoff)
        },
    )

    assert result[0].institutional_data_cutoff is cutoff


def test_build_report_stocks_carries_relative_sample_size_and_rank_through():
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
            relative_sample_size={"liquidity": 12},
            relative_rank={"liquidity": 1},
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].relative_sample_size == {"liquidity": 12}
    assert result[0].relative_rank == {"liquidity": 1}


def test_build_report_stocks_defaults_absolute_signal_fields_when_absent():
    """A ScoredStock built without relative_sample_size/relative_rank
    (e.g. an older test fixture) must still flow through
    build_report_stocks without error, defaulting to {}."""
    scored = [
        ScoredStock(
            stock_id="1101",
            total_score=80.0,
            factor_scores={"liquidity": 90.0},
            risk_flags=(),
            data_completeness=0.90,
        )
    ]
    candidate = _make_candidate(stock_id="1101")

    result = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"1101": candidate.stock},
        candidates={"1101": candidate},
        features_by_stock={"1101": _make_features("1101")},
    )

    assert result[0].institutional_data_cutoff is None
    assert result[0].relative_sample_size == {}
    assert result[0].relative_rank == {}


# --- Step 8: end-to-end regression tests -------------------------------------
#
# The tests above (and Step 5/6's own unit tests) each verify their own
# layer in isolation — build_report_stocks() carries fields through
# correctly, text_renderer renders a hand-built ReportStockView
# correctly. Neither proves the two layers are actually wired together
# correctly when driven by the SAME real objects a live run would
# produce. These tests close that gap by running the full
# ScoredStock -> build_report_stocks() -> render_daily_report() chain,
# exactly the sequence app.jobs.daily_ranking uses in production.


def test_3022_institutional_absolute_signal_never_green_end_to_end():
    """
    Named regression test straight from the requirements doc: stock
    "3022" on trading date 2026/09/04, with a 5-day institutional
    net-buy ratio of -6.3% that nonetheless ranks 1st (percentile 100)
    in an unusually small 3-stock candidate pool. Run through the REAL
    ScoredStock -> build_report_stocks -> render_daily_report chain
    (not a hand-built ReportStockView) to prove Steps 4/5/6 are
    correctly wired together end-to-end, not just individually
    correct in isolation.

    Absolute Signal must show 🔴 (never 🟢), Relative Score must still
    be visible (100/100, with the small pool correctly triggering the
    rank-only degrade from Step 6), and the T-1 cutoff must be stated
    explicitly.
    """
    trading_date = dt.date(2026, 9, 4)
    cutoff = InstitutionalDataCutoff(
        expected_as_of_date=dt.date(2026, 9, 3),
        confirmed_as_of_date=dt.date(2026, 9, 3),
    )

    scored = [
        ScoredStock(
            stock_id="3022",
            total_score=76.4,
            data_completeness=1.0,
            factor_scores={
                "liquidity": 80.0,
                "volume_price": 75.0,
                "momentum": 70.0,
                "institutional": 100.0,  # best of the 3-stock pool...
                "fundamental": 50.0,
                "risk_quality": 90.0,
            },
            risk_flags=(),
            relative_sample_size={"institutional": 3},
            relative_rank={"institutional": 1},
        )
    ]
    candidate = _make_candidate(
        stock_id="3022",
        stock_name="測試光罩",
        open_price="50",
        high_price="55",
        low_price="50",
        close_price="55",
        reference_price="50",
        limit_up_price="55",
    )
    features = StockFeatures(
        stock_id="3022",
        turnover=200_000_000.0,
        average_turnover_20d=150_000_000.0,
        volume_ratio_20d=1.8,
        return_5d=0.06,
        return_20d=0.15,
        institutional_net_buy_ratio_5d=-0.063,  # ...but still a net sell
        institutional_data_cutoff=cutoff,
        revenue_yoy=0.05,
        risk_quality_raw=0.9,
    )

    report_stocks = build_report_stocks(
        ranked_stocks=scored,
        stock_master={"3022": candidate.stock},
        candidates={"3022": candidate},
        features_by_stock={"3022": features},
    )

    report = render_daily_report(
        trading_date=trading_date,
        data_updated_at="16:47",
        candidate_count=3,
        eligible_count=3,
        strategy_version="rule-v1.2.0",
        ranked_stocks=report_stocks,
        ranking_limit=10,
    )

    assert "🟢 籌碼：強" not in report
    assert "🔴 籌碼：偏弱" in report
    assert "候選池相對：第 1 / 3（樣本偏少）" in report
    assert "法人資料截止：T-1（2026/09/03）" in report
    # the composite score is untouched by any of this — see the
    # sibling test below for the more general version of this check.
    assert "綜合分數：76.40" in report


def test_total_score_unaffected_by_absolute_signal_rendering_end_to_end():
    """
    Item 17 from the requirements doc's completion checklist,
    end-to-end version of Step 4's scoring-layer-only test: with the
    SAME ScoredStock.total_score/factor_scores, changing only the
    Absolute-Signal-relevant raw inputs (institutional_net_buy_ratio_5d
    swung from positive to sharply negative) must not change the
    rendered "綜合分數" line at all — Absolute Signal is a rendering
    concern layered entirely on top of scoring, never feeding back
    into it.
    """

    def render_with(institutional_net_buy_ratio_5d: float | None) -> str:
        scored = [
            ScoredStock(
                stock_id="1101",
                total_score=88.8,
                data_completeness=1.0,
                factor_scores={
                    "liquidity": 90.0,
                    "volume_price": 85.0,
                    "momentum": 80.0,
                    "institutional": 90.0,
                    "fundamental": 90.0,
                    "risk_quality": 90.0,
                },
                risk_flags=(),
            )
        ]
        candidate = _make_candidate(stock_id="1101")
        features = _make_features(
            "1101", institutional_net_buy_ratio_5d=institutional_net_buy_ratio_5d
        )
        report_stocks = build_report_stocks(
            ranked_stocks=scored,
            stock_master={"1101": candidate.stock},
            candidates={"1101": candidate},
            features_by_stock={"1101": features},
        )
        return render_daily_report(
            trading_date=TRADING_DATE,
            data_updated_at="16:47",
            candidate_count=1,
            eligible_count=1,
            strategy_version="rule-v1.2.0",
            ranked_stocks=report_stocks,
            ranking_limit=10,
        )

    report_positive = render_with(0.03)
    report_negative = render_with(-0.09)

    assert "🟢 籌碼：強" in report_positive
    assert "🔴 籌碼：偏弱" in report_negative
    # despite the opposite Absolute Signal, both reports show the
    # exact same composite score, unaffected by which raw institutional
    # value was used to derive the light.
    assert "綜合分數：88.80" in report_positive
    assert "綜合分數：88.80" in report_negative
    assert sum(FACTOR_WEIGHTS.values()) == pytest.approx(1.0)
