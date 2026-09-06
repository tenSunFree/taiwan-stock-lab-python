from app.domain.absolute_signal import (
    ABSOLUTE_GATED_FACTORS,
    AbsoluteSignal,
    fundamental_absolute_signal,
    institutional_absolute_signal,
)


# --- institutional_absolute_signal ------------------------------------------


def test_institutional_positive_is_green():
    assert institutional_absolute_signal(0.001) == AbsoluteSignal("🟢", "強")


def test_institutional_zero_is_yellow():
    assert institutional_absolute_signal(0.0) == AbsoluteSignal("🟡", "普通")


def test_institutional_negative_is_red():
    """Core intent: even if this value ranks best in the candidate pool
    (e.g. the only positive value, or the smallest decline), an
    absolute negative must still show 🔴, never 🟢. This test only
    verifies the gate function itself; the integrated scenario where a
    pool-top rank still shows a red light will be covered by
    regression tests once this is wired into text_renderer."""
    assert institutional_absolute_signal(-0.063) == AbsoluteSignal("🔴", "偏弱")


def test_institutional_missing_is_unknown():
    assert institutional_absolute_signal(None) == AbsoluteSignal("⚪", "資料不足")


# --- fundamental_absolute_signal --------------------------------------------


def test_fundamental_ten_percent_is_green():
    assert fundamental_absolute_signal(0.10) == AbsoluteSignal("🟢", "強")


def test_fundamental_above_ten_percent_is_green():
    assert fundamental_absolute_signal(0.25) == AbsoluteSignal("🟢", "強")


def test_fundamental_zero_to_ten_percent_is_yellow():
    assert fundamental_absolute_signal(0.0) == AbsoluteSignal("🟡", "普通")
    assert fundamental_absolute_signal(0.099) == AbsoluteSignal("🟡", "普通")


def test_fundamental_negative_is_red():
    assert fundamental_absolute_signal(-0.01) == AbsoluteSignal("🔴", "偏弱")


def test_fundamental_missing_is_unknown():
    assert fundamental_absolute_signal(None) == AbsoluteSignal("⚪", "資料不足")


def test_absolute_gated_factors():
    """This rollout explicitly covers only these two factors.
    text_renderer will branch on this constant when rendering, so lock
    the set here to prevent accidental future expansion/contraction."""
    assert ABSOLUTE_GATED_FACTORS == frozenset({"institutional", "fundamental"})
