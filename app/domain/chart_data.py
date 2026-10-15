"""
Chart projection — a PURE view of price history prepared for drawing.

Responsibility boundary (LINE chart spec §二):
    * This module only ASSEMBLES: it slices the series, computes moving
      averages and the prior-volume average, and attaches the 20-day
      range. It makes NO strategy decision — no threshold, no "低檔",
      no "首板", no "過熱". Those come from the Rule Engine
      (technical_signal_builder, momentum_signal) and travel next to this
      object on the report model.
    * The 20-day range is NOT computed here: it comes from
      app.domain.price_structure.build_range_20d, the same function the
      低檔首板 signal uses, so the dashed High/Low lines and the signal
      can never disagree.

Series conventions:
    * Input is HistoricalPricePoint for sessions strictly before T plus
      today's point. Today's point must come from the official TWSE/TPEx
      daily data, not FinMind; this module does not care where it came
      from, only that it is a coherent candle dated T.
    * Up to FETCH_TRADING_DAYS (120) sessions are kept; MAs are computed
      over ALL of them and only then sliced to the last DISPLAY_BARS (60),
      so MA60 is already correct on the first displayed bar.
    * Fewer sessions than a window -> that MA stays None until it exists.
      Nothing is padded, copied from the previous day, or extrapolated.
    * avg_volume_20[i] = mean volume of the 20 sessions strictly BEFORE
      bar i (bar i excluded) — the same denominator
      app.domain.feature_builder uses for volume_ratio_20d, so the last
      point of the line is exactly what the card's "量比" is divided by.
    * Every DISPLAYED bar must be a coherent OHLC candle; otherwise the
      chart is unavailable (None) and the caller renders a card without a
      hero image. Warm-up bars outside the display window only need a
      valid close (for the MAs).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

from app.domain.feature_builder import HistoricalPricePoint
from app.domain.models import DailyPrice
from app.domain.price_structure import (
    PriceRange20d,
    build_range_20d,
    build_valid_history,
    has_valid_candle,
)

DISPLAY_BARS = 60
FETCH_TRADING_DAYS = 120  # 60 displayed + MA60 warm-up (+ buffer)
MA_WINDOWS = (5, 20, 60)
VOLUME_AVG_WINDOW = 20


@dataclass(frozen=True)
class StockChartData:
    bars: tuple[HistoricalPricePoint, ...]  # <= DISPLAY_BARS, last bar is T
    ma5: tuple[float | None, ...]
    ma20: tuple[float | None, ...]
    ma60: tuple[float | None, ...]
    avg_volume_20: tuple[float | None, ...]
    range_20d: PriceRange20d | None  # T-20..T-1, None if not computable


def today_point_from_daily_price(price: DailyPrice) -> HistoricalPricePoint | None:
    """Today's (T) candle from the OFFICIAL TWSE/TPEx daily data.

    FinMind is deliberately never the source of today's bar (its
    aggregation can lag the exchange feeds on the same day). Any missing
    OHLCV/turnover field -> None: the chart is then unavailable rather
    than drawn from a guessed candle.
    """
    required = (
        price.open_price,
        price.high_price,
        price.low_price,
        price.close_price,
        price.volume,
        price.turnover,
    )
    if any(value is None for value in required):
        return None
    return HistoricalPricePoint(
        trading_date=price.trading_date,
        open=float(price.open_price),
        high=float(price.high_price),
        low=float(price.low_price),
        close=float(price.close_price),
        volume=float(price.volume),
        turnover=float(price.turnover),
    )


def moving_average_series(values: Sequence[float], window: int) -> list[float | None]:
    """Simple MA aligned 1:1 with ``values``; None until ``window`` values."""
    result: list[float | None] = []
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        result.append(running / window if index >= window - 1 else None)
    return result


def prior_average_series(
    values: Sequence[float], window: int = VOLUME_AVG_WINDOW
) -> list[float | None]:
    """result[i] = mean(values[i-window : i]); bar i itself is excluded."""
    return [
        sum(values[i - window : i]) / window if i >= window else None
        for i in range(len(values))
    ]


def build_stock_chart_data(
    *,
    history: Sequence[HistoricalPricePoint],
    today: HistoricalPricePoint,
    display_bars: int = DISPLAY_BARS,
    fetch_trading_days: int = FETCH_TRADING_DAYS,
) -> StockChartData | None:
    """Return the drawable series, or None when no valid chart exists
    (invalid today candle, duplicate-date history, or an incoherent /
    missing OHLC in the displayed window)."""
    if today.close <= 0 or not has_valid_candle(today):
        return None
    prior = build_valid_history(history, target_date=today.trading_date)
    if prior is None:
        return None

    prior = prior[-(fetch_trading_days - 1) :]
    full = prior + [today]

    ma = {
        window: moving_average_series([p.close for p in full], window)
        for window in MA_WINDOWS
    }
    avg_volume = prior_average_series([p.volume for p in full])

    start = max(0, len(full) - display_bars)
    shown = full[start:]
    if not all(has_valid_candle(p) and p.volume >= 0 for p in shown):
        return None

    return StockChartData(
        bars=tuple(shown),
        ma5=tuple(ma[5][start:]),
        ma20=tuple(ma[20][start:]),
        ma60=tuple(ma[60][start:]),
        avg_volume_20=tuple(avg_volume[start:]),
        range_20d=build_range_20d(prior),
    )
