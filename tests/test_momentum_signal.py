import pytest

from app.domain.momentum_signal import (
    WEAK_MOMENTUM_SCORE_THRESHOLD,
    build_momentum_overheated_status,
)
from app.domain.signal_status import SignalStatus
from app.reports.text_renderer import _WEAK_SIGNAL_THRESHOLD

FLAG = ("HIGH_FIVE_DAY_RETURN",)


def test_signal_status_from_optional_bool():
    assert SignalStatus.from_optional_bool(True) is SignalStatus.TRUE
    assert SignalStatus.from_optional_bool(False) is SignalStatus.FALSE
    assert SignalStatus.from_optional_bool(None) is SignalStatus.INSUFFICIENT_DATA


@pytest.mark.parametrize(
    "score, flags, expected",
    [
        (10.0, FLAG, SignalStatus.TRUE),
        (39.99, FLAG, SignalStatus.TRUE),
        (40.0, FLAG, SignalStatus.FALSE),  # boundary: 40 is "普通", not 偏弱
        (85.0, FLAG, SignalStatus.FALSE),  # flag fired but score is strong
        (10.0, (), SignalStatus.FALSE),  # weak but not overheated
        (None, FLAG, SignalStatus.INSUFFICIENT_DATA),
        (None, (), SignalStatus.INSUFFICIENT_DATA),
    ],
)
def test_overheated_status(score, flags, expected):
    assert (
        build_momentum_overheated_status(momentum_score=score, risk_flags=flags)
        is expected
    )


def test_text_report_and_domain_share_the_same_threshold():
    assert _WEAK_SIGNAL_THRESHOLD == WEAK_MOMENTUM_SCORE_THRESHOLD


@pytest.mark.parametrize("score", [None, 0.0, 10.0, 39.99, 40.0, 70.0, 100.0])
@pytest.mark.parametrize("flags", [(), FLAG, ("KY_STOCK",) + FLAG])
def test_every_consumer_agrees_with_the_single_domain_rule(score, flags):
    """The text report's level word, the explainer's reason and
    ScoredStock all derive from build_momentum_overheated_status — none
    carries its own copy of the threshold. If someone re-hard-codes
    `score < 40` anywhere, this grid starts to disagree."""
    from app.domain.scoring import ScoredStock
    from app.reports.signal_explainer import explain_momentum
    from app.reports.text_renderer import _momentum_signal_word

    expected = build_momentum_overheated_status(momentum_score=score, risk_flags=flags)
    overheated = expected is SignalStatus.TRUE

    assert (_momentum_signal_word(score, flags) == "漲多過熱") is overheated

    explanation = explain_momentum(
        return_5d=0.2 if score is not None else None,
        return_20d=None,
        score=score,
        risk_flags=flags,
    )
    assert any("過熱扣分區" in reason for reason in explanation.reasons) is overheated

    stock = ScoredStock(
        stock_id="1101",
        total_score=1.0,
        data_completeness=1.0,
        factor_scores={"momentum": score},
        risk_flags=flags,
    )
    assert stock.momentum_overheated_status is expected
