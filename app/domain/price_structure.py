"""
Objective price-history structure — the SINGLE implementation of the
20-session High/Low range and the pre-limit-up position inside it.

This module answers only "what does the price history look like?". It
contains NO threshold and makes NO strategy decision: whether a position
counts as "低檔" is decided by the Rule Engine
(app.domain.technical_signal_builder.RANGE_POSITION_LOW_THRESHOLD). Chart
and Flex layers must read these results (directly, or via the signal /
chart-data objects built from them) and never re-implement them.

Definitions (T = the limit-up trading day being reported):

* 20-day range = sessions T-20 .. T-1. TODAY IS EXCLUDED, so today's
  limit-up High can never stretch the "before the event" background:
      range_20d_high = max(High[T-20 .. T-1])
      range_20d_low  = min(Low [T-20 .. T-1])
* Position is measured at T-1, not at today's limit-up close:
      position = (Close[T-1] - range_20d_low) / (range_20d_high - range_20d_low)
  0 ~= bottom of the prior-20-session range, 1 ~= top.
* Strict window: fewer than RANGE_WINDOW valid sessions, any session in
  the window lacking a usable high/low, or a flat range
  (high == low, would divide by zero) all yield None — the same
  "None over a fabricated number" policy as app.domain.feature_builder.

``history`` arguments below must already be cleaned (see
build_valid_history): ascending, strictly before T, one row per date.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from app.domain.feature_builder import HistoricalPricePoint

RANGE_WINDOW = 20


@dataclass(frozen=True)
class PriceRange20d:
    high: float  # max(High[T-20 .. T-1])
    low: float  # min(Low[T-20 .. T-1])


@dataclass(frozen=True)
class LowLevelPosition:
    """position is None whenever it cannot be computed; range_20d is
    still returned when the range itself exists (e.g. flat range)."""

    position: float | None
    range_20d: PriceRange20d | None


def build_valid_history(
    history: Sequence[HistoricalPricePoint], *, target_date: dt.date
) -> list[HistoricalPricePoint] | None:
    """No-look-ahead / duplicate-date defense shared by every consumer.

    Rows on or after target_date and non-positive closes are dropped. A
    duplicate trading_date makes the WHOLE series untrustworthy (we cannot
    tell which row is corrupted) -> None, never partial data.
    """
    by_date: dict[dt.date, HistoricalPricePoint] = {}
    for point in history:
        if point.trading_date >= target_date or point.close <= 0:
            continue
        if point.trading_date in by_date:
            return None
        by_date[point.trading_date] = point
    return sorted(by_date.values(), key=lambda point: point.trading_date)


def has_valid_candle(point: HistoricalPricePoint) -> bool:
    """True when open/high/low/close form a coherent candle."""
    if point.open is None or point.high is None or point.low is None:
        return False
    if min(point.open, point.high, point.low, point.close) <= 0:
        return False
    return (
        point.high >= point.low
        and point.high >= max(point.open, point.close)
        and point.low <= min(point.open, point.close)
    )


def build_range_20d(history: Sequence[HistoricalPricePoint]) -> PriceRange20d | None:
    if len(history) < RANGE_WINDOW:
        return None
    window = history[-RANGE_WINDOW:]
    highs: list[float] = []
    lows: list[float] = []
    for point in window:
        if point.high is None or point.low is None:
            return None  # strict window: never fall back to close
        if point.high <= 0 or point.low <= 0 or point.high < point.low:
            return None
        highs.append(point.high)
        lows.append(point.low)
    return PriceRange20d(high=max(highs), low=min(lows))


def build_low_level_position(
    history: Sequence[HistoricalPricePoint],
) -> LowLevelPosition:
    range_20d = build_range_20d(history)
    if range_20d is None:
        return LowLevelPosition(position=None, range_20d=None)
    span = range_20d.high - range_20d.low
    if span <= 0:
        return LowLevelPosition(position=None, range_20d=range_20d)
    position = (history[-1].close - range_20d.low) / span
    return LowLevelPosition(position=position, range_20d=range_20d)
