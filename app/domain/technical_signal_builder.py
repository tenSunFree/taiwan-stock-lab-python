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

SECOND SIGNAL — build_low_first_limit_up_signal() ("低檔首板"):
    Per this module's own design decision above ("a second signal
    type ... should carry its own reason, not be silently OR'd into
    this one"), this is a SEPARATE function with its own tri-state
    result, not merged into build_low_with_rising_signal(). It
    answers: "is today's close (a) the legal limit-up price, (b) near
    the low end of its own trailing 20-session range, and (c) NOT a
    continuation of a limit-up that already happened on the
    immediately preceding trading session."

    (c) requires knowing whether the PREVIOUS session itself closed
    at ITS OWN legal limit-up price. This project deliberately does
    NOT reconstruct a general consecutive_limit_up_days count from
    raw closes (see app.domain.risk_inputs's module docstring) because
    "previous close * 1.10" is unreliable on ex-rights/ex-dividend/
    capital-reduction days — precisely the days it matters most for a
    RiskPolicy scoring input. estimate_previous_session_limit_up()
    below uses that exact same approximation, but for a narrower,
    explicitly-labeled PROVISIONAL purpose: a single-day, DISPLAY-ONLY
    hint, never fed into RiskPolicy, consecutive_limit_up_days, or any
    FACTOR_WEIGHTS scoring factor. Its name says "estimate", not "is",
    on purpose — a future caller must not mistake it for an
    authoritative determination the way app.domain.limit_up.evaluate_limit_up
    is.

    EXPLAINABILITY: build_low_first_limit_up_signal() returns a
    LowFirstLimitUpSignal dataclass, not a bare bool | None — mirroring
    this project's own explainable-signals lesson from the six scoring
    factors (see app.reports.signal_explainer), applied here even
    though this remains a non-scoring, DISPLAY-ONLY signal. Each
    sub-check (is_low, previous_session_limit_up_estimated) is
    resolved INDEPENDENTLY where possible: e.g. a stock with fewer
    than RANGE_WINDOW valid trailing sessions can't have is_low
    computed, but MAY still have enough sessions (just 2) for
    previous_session_limit_up_estimated to resolve — the dataclass
    surfaces that partial picture instead of collapsing everything to
    a single None the moment ANY one piece is unknown. `matched` is
    the overall tri-state result callers that only need the old bare
    bool should read (e.g. app.reports.text_renderer's rendered "低檔
    首板：是／否／資料不足" line uses ONLY this field — the report text
    is unchanged by this dataclass refactor).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.domain.feature_builder import HistoricalPricePoint
from app.domain.price_ticks import calculate_limit_up_price

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
    A duplicate trading_date in the input is ALSO treated as invalid
    (returns None) rather than silently letting it count twice toward
    RANGE_WINDOW — mirroring
    app.domain.institutional_flow_builder's own duplicate-date
    defense, since counting rows instead of distinct sessions could
    otherwise let a diluted/incorrect window pass the "enough data"
    gate.

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

    valid_history = _build_valid_history(history, target_date=target_date)
    if valid_history is None:
        return None  # duplicate trading_date found — see helper docstring

    if len(valid_history) < RANGE_WINDOW:
        return None

    is_low = _range_position(valid_history, today_close=today_close) <= (
        RANGE_POSITION_LOW_THRESHOLD
    )
    is_rising = _bullish_ma5_crossover(valid_history, today_close=today_close)

    return is_low and is_rising


@dataclass(frozen=True)
class LowFirstLimitUpSignal:
    """
    Structured breakdown behind build_low_first_limit_up_signal()'s
    "低檔首板" determination — see that function's own docstring for
    the full rule. Exists so a caller (report renderer, backtest,
    debugger) can see WHY `matched` came out the way it did, without
    recomputing is_low / the previous-session estimate independently.

    matched: the overall tri-state result. True only when
        is_close_limit_up, is_low, and "not
        previous_session_limit_up_estimated" all hold; False when
        is_close_limit_up is False (short-circuit — see
        build_low_first_limit_up_signal's own docstring for why is_low/
        previous_session_limit_up_estimated are never even attempted
        in that case) or when all three were resolved but at least one
        didn't hold; None when is_close_limit_up is True but is_low
        and/or previous_session_limit_up_estimated couldn't be
        resolved.

    is_low: RANGE_POSITION_LOW_THRESHOLD range-position check result,
        or None if there wasn't enough valid trailing history (fewer
        than RANGE_WINDOW distinct sessions, or a duplicate
        trading_date) to compute the RANGE_WINDOW-session range at
        all. Always None when is_close_limit_up is False.

    range_position: the raw (today_close - low) / (high - low) value
        feeding is_low — kept for debugging/backtesting even though
        the rendered report only needs the boolean. Same None
        conditions as is_low.

    is_close_limit_up: today's OWN legal limit-up determination,
        carried straight through from the caller
        (app.domain.limit_up.evaluate_limit_up's
        LimitUpResult.is_close_limit_up) — never recomputed here, and
        never None (the caller must always know this before calling).

    previous_session_limit_up_estimated: whether the PREVIOUS trading
        session itself closed at its own (approximated) legal
        limit-up price, per estimate_previous_session_limit_up(). None
        when there wasn't enough valid trailing history (fewer than 2
        distinct sessions, or a duplicate trading_date) to estimate it
        at all. Resolved INDEPENDENTLY of is_low — a stock with fewer
        than RANGE_WINDOW valid sessions can still have this field
        resolved as long as at least 2 valid sessions exist, since
        this check only ever needs the 2 most recent ones. Always None
        when is_close_limit_up is False.

    previous_session_check_provisional: always True — a fixed marker
        (not a computed result) so any downstream consumer inspecting
        this dataclass in isolation (a log line, a backtest export)
        can see at a glance that previous_session_limit_up_estimated
        is an approximation, without having to go find this module's
        docstring. See estimate_previous_session_limit_up's own
        docstring for exactly which days it is wrong on.
    """

    matched: bool | None
    is_low: bool | None
    range_position: float | None
    is_close_limit_up: bool
    previous_session_limit_up_estimated: bool | None
    previous_session_check_provisional: bool = True


def build_low_first_limit_up_signal(
    *,
    target_date: dt.date,
    today_close: float,
    is_close_limit_up: bool,
    history: list[HistoricalPricePoint],
) -> LowFirstLimitUpSignal:
    """
    "低檔首板": whether today is ALL of:

    (a) 漲停 — today's close is the legal limit-up price. This is
        NOT recomputed here — is_close_limit_up must come from the
        SAME authoritative source CandidateBuilder itself relies on
        (app.domain.limit_up.evaluate_limit_up's
        LimitUpResult.is_close_limit_up), never reconstructed from
        raw closes. This function only combines that already-trusted
        fact with the two checks below.

    (b) 低檔 — same RANGE_WINDOW/RANGE_POSITION_LOW_THRESHOLD trailing-
        range check as build_low_with_rising_signal() above.

    (c) 首板 — the PREVIOUS trading session did NOT itself close at
        its own legal limit-up price, per
        estimate_previous_session_limit_up()'s PROVISIONAL,
        single-day approximation (see this module's docstring for why
        it is only ever a single day, never a general
        consecutive_limit_up_days reconstruction).

    Returns a LowFirstLimitUpSignal (see that dataclass's own
    docstring for the full field-by-field breakdown), NOT a bare
    bool | None — this module's own "EXPLAINABILITY" docstring section
    explains why. Callers that only need the old tri-state bool should
    read `.matched`.

    Short-circuits on (a) first: if is_close_limit_up is False, the
    overall result is definitively False regardless of whether (b)/(c)
    could even be computed — (b) and (c) are not even attempted in
    that case (both come back None in the returned dataclass), since
    there is no trailing-history requirement to satisfy when the
    conjunction is already known to be False.

    Once (a) is True, (b) and (c) are each resolved INDEPENDENTLY —
    see LowFirstLimitUpSignal.previous_session_limit_up_estimated's
    own docstring for why a stock can have (c) resolved even when (b)
    can't be (fewer than RANGE_WINDOW valid sessions but at least 2).
    `matched` is None whenever either resolves to None; True only when
    both resolve and both hold; False when both resolve and at least
    one doesn't.
    """
    if not is_close_limit_up:
        return LowFirstLimitUpSignal(
            matched=False,
            is_low=None,
            range_position=None,
            is_close_limit_up=False,
            previous_session_limit_up_estimated=None,
        )

    if today_close <= 0:
        return LowFirstLimitUpSignal(
            matched=None,
            is_low=None,
            range_position=None,
            is_close_limit_up=True,
            previous_session_limit_up_estimated=None,
        )

    valid_history = _build_valid_history(history, target_date=target_date)
    if valid_history is None:
        # Duplicate trading_date found — the whole window is
        # untrustworthy (we can't tell which entries are corrupted),
        # so BOTH sub-checks stay unresolved, not just the range one.
        return LowFirstLimitUpSignal(
            matched=None,
            is_low=None,
            range_position=None,
            is_close_limit_up=True,
            previous_session_limit_up_estimated=None,
        )

    range_position: float | None = None
    is_low: bool | None = None
    if len(valid_history) >= RANGE_WINDOW:
        range_position = _range_position(valid_history, today_close=today_close)
        is_low = range_position <= RANGE_POSITION_LOW_THRESHOLD

    previous_session_limit_up = estimate_previous_session_limit_up(valid_history)

    matched: bool | None
    if is_low is None or previous_session_limit_up is None:
        matched = None
    else:
        matched = is_low and not previous_session_limit_up

    return LowFirstLimitUpSignal(
        matched=matched,
        is_low=is_low,
        range_position=range_position,
        is_close_limit_up=True,
        previous_session_limit_up_estimated=previous_session_limit_up,
    )


def estimate_previous_session_limit_up(
    valid_history: list[HistoricalPricePoint],
) -> bool | None:
    """
    PROVISIONAL, single-day, DISPLAY-ONLY estimate of whether the
    immediately preceding trading session (valid_history[-1]) itself
    closed at ITS OWN legal limit-up price.

    Approximates that session's opening reference price as the close
    of the session BEFORE it (valid_history[-2]) — the exact same
    provisional convention app.ingestion.finmind_mapper already uses
    for TODAY's reference_price when no official source is wired in —
    then runs it through the same tick-rounding rule
    (app.domain.price_ticks.calculate_limit_up_price) real limit-up
    determination uses. This is wrong on the same days that
    approximation is always wrong (ex-rights/ex-dividend days,
    capital reductions, newly listed securities) — see
    app.domain.risk_inputs's module docstring for why the project does
    NOT feed a value computed this way into RiskPolicy or
    treat it as a real consecutive-limit-up count. It is only ever
    consumed by build_low_first_limit_up_signal() above, purely to
    decide what to display in a non-scoring report block, and every
    consumer must keep treating a True/False result here as an
    approximation, not a confirmed historical fact.

    Returns None when there aren't at least 2 valid_history points to
    compare (mirrors this module's other "not enough data" gates —
    never guessed from a single point).
    """
    if len(valid_history) < 2:
        return None

    previous_close = valid_history[-1].close
    reference_close = valid_history[-2].close

    if previous_close <= 0 or reference_close <= 0:
        return None

    try:
        approximate_limit_up_price = calculate_limit_up_price(
            Decimal(str(reference_close))
        )
    except (InvalidOperation, ValueError):
        return None

    return Decimal(str(previous_close)) == approximate_limit_up_price


def _build_valid_history(
    history: list[HistoricalPricePoint], *, target_date: dt.date
) -> list[HistoricalPricePoint] | None:
    """
    Shared no-look-ahead / duplicate-date defense for every signal in
    this module (see build_low_with_rising_signal's own docstring for
    the full rationale): rows on or after target_date and non-positive
    closes are dropped, and a duplicate trading_date is treated as
    invalid input — returns None (not partial data) rather than
    silently letting a diluted/incorrect window pass any "enough data"
    gate, mirroring app.domain.institutional_flow_builder's own
    duplicate-date defense.
    """
    valid_history_by_date: dict[dt.date, HistoricalPricePoint] = {}
    for point in history:
        if point.trading_date >= target_date or point.close <= 0:
            continue
        if point.trading_date in valid_history_by_date:
            return None
        valid_history_by_date[point.trading_date] = point

    return sorted(valid_history_by_date.values(), key=lambda point: point.trading_date)


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
