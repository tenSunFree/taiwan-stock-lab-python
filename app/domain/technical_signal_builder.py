"""
Pure, provider-independent low-position + early-rally technical signal.

DESIGN DECISION — v1 scope, deliberately narrow: this is a single,
precisely-defined DISPLAY-ONLY signal, not a general-purpose technical
indicator library. It answers exactly one question — "is today's
close both (a) near the low end of its own recent 20-session trading
range, AND (b) has it just crossed above its own 5-session moving
average for the first time (not merely already sitting above it)" —
and nothing else. It deliberately does NOT also check a
return-reversal-style signal (5-day return turning positive while
20-day return is still negative) as an alternative OR condition in
this version: mixing two different signal shapes into one boolean
would make a True result ambiguous ("which condition fired?"). If a
second signal type is added later, it should carry its own reason
(e.g. a technical_signal_reason enum), not be silently OR'd into this
one.

This is NOT a scoring factor: it does not participate in
FACTOR_WEIGHTS, RiskPolicy, or bounded_momentum_score in any way — see
app.reports.text_renderer's "技術面" block for where this is
displayed.

Reuses app.domain.feature_builder.HistoricalPricePoint directly rather
than defining a parallel point type, since the input data (trading
date + close) is identical to what build_price_features() already
consumes from the very same historical fetch — no second data source
or API call is needed.
"""

from __future__ import annotations

import datetime as dt

from app.domain.feature_builder import HistoricalPricePoint

RANGE_WINDOW = 20
RANGE_POSITION_LOW_THRESHOLD = 0.30
MOVING_AVERAGE_WINDOW = 5


def build_low_with_rising_signal(
    *,
    target_date: dt.date,
    today_close: float,
    history: list[HistoricalPricePoint],
) -> bool | None:
    """
    Whether today's close is BOTH:

    (a) "低檔" — within the bottom RANGE_POSITION_LOW_THRESHOLD
        (default 30%) of the trailing RANGE_WINDOW-session (default
        20) closing-price range, i.e.
        (today_close - low) / (high - low) <= threshold; and

    (b) "起漲" — a bullish MOVING_AVERAGE_WINDOW-session (default
        5-day) moving-average CROSSOVER as of today: the PREVIOUS
        trading day's close was at or below its own 5-day moving
        average, and TODAY's close is strictly above today's 5-day
        moving average. This is deliberately a crossover check, not a
        "close > MA5" snapshot check — a stock that has already been
        sitting above its MA5 for two weeks is "already strong," not
        "just starting to rise," and this signal must not conflate
        the two.

    Returns True only when both conditions hold, False when both
    required calculations succeeded but at least one condition did
    not hold, and None when there isn't enough valid trailing history
    to compute the RANGE_WINDOW-session range at all (this project's
    usual fail-closed tri-state convention — never silently computed
    from a partial window, and never treated the same as a confirmed
    "no"). RANGE_WINDOW (20) is always >= MOVING_AVERAGE_WINDOW + 1
    (6), so once the range check has enough data, the crossover check
    always does too — there is only one "insufficient data" gate, not
    two independently-reported ones.

    Same no-look-ahead defensiveness as build_price_features(): rows
    on or after target_date are discarded even if the caller included
    them by mistake, and only positive closes are considered valid.

    DEFENSIVE, not just caller-trusting: a non-positive today_close
    also returns None immediately, even though CandidateBuilder should
    already guarantee a positive close by the time this function is
    called. This project's safety properties don't rely solely on
    caller discipline (see e.g. build_price_features's and
    CandidateBuilder's own docstrings for the same principle) — a
    zero/negative close would otherwise corrupt both the range
    position and the moving-average comparison without raising.
    """
    if today_close <= 0:
        return None

    valid_history = sorted(
        (
            point
            for point in history
            if point.trading_date < target_date and point.close > 0
        ),
        key=lambda point: point.trading_date,
    )

    if len(valid_history) < RANGE_WINDOW:
        return None

    is_low = _range_position(valid_history, today_close=today_close) <= (
        RANGE_POSITION_LOW_THRESHOLD
    )
    is_rising = _bullish_ma5_crossover(valid_history, today_close=today_close)

    return is_low and is_rising


def _range_position(
    valid_history: list[HistoricalPricePoint], *, today_close: float
) -> float:
    trailing_closes = [point.close for point in valid_history[-RANGE_WINDOW:]]
    low_20d = min(trailing_closes)
    high_20d = max(trailing_closes)

    if high_20d == low_20d:
        # A perfectly flat 20-session range: today's close can only be
        # at, above, or below that single price. Treat "at or below"
        # as the bottom of this degenerate range and "above" as the
        # top, rather than dividing by zero.
        return 0.0 if today_close <= low_20d else 1.0

    return (today_close - low_20d) / (high_20d - low_20d)


def _moving_average(closes: list[float]) -> float:
    return sum(closes) / len(closes)


def _bullish_ma5_crossover(
    valid_history: list[HistoricalPricePoint], *, today_close: float
) -> bool:
    # "Today's MA5" = the 4 most recent historical closes + today's
    # own close.
    today_ma5 = _moving_average(
        [point.close for point in valid_history[-(MOVING_AVERAGE_WINDOW - 1) :]]
        + [today_close]
    )

    # "Previous trading day's MA5" = the 5 most recent historical
    # closes as of that previous day — valid_history's own last entry
    # IS the previous trading day, so this is simply the trailing-5
    # window of valid_history itself.
    previous_close = valid_history[-1].close
    previous_ma5 = _moving_average(
        [point.close for point in valid_history[-MOVING_AVERAGE_WINDOW:]]
    )

    return previous_close <= previous_ma5 and today_close > today_ma5
