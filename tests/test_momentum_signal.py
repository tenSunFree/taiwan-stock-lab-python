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
