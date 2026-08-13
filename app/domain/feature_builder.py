"""
Pure, provider-independent historical price/volume feature
calculations — 20-day average turnover/volume, volume ratio, 5-day
and 20-day return.

This module has no knowledge of FinMind, TWSE, or TPEx field names.
Provider-specific adapters (see app.ingestion.finmind_mapper's
build_historical_price_points()) convert raw source rows into
HistoricalPricePoint before calling into this module.

DESIGN DECISION — self-defending against look-ahead bias: even though
callers are expected to only pass historical sessions strictly before
target_date, build_price_features() also defensively filters by
target_date itself rather than trusting the caller unconditionally.
This project's other modules (CandidateBuilder, DeliveryRepository,
etc.) follow the same principle — safety properties should not depend
solely on caller discipline.

DESIGN DECISION — strict windows, not best-effort: if any session
inside the trailing 20-trading-day window is missing a required
value, the corresponding feature is None for the whole window, rather
than silently computing an average over fewer/non-contiguous points.
This matches the project's existing philosophy (see
app.domain.scoring's missing-factor handling) of never fabricating a
plausible-looking number from incomplete data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

TRAILING_WINDOW = 20


@dataclass(frozen=True)
class HistoricalPricePoint:
    trading_date: dt.date
    close: float
    volume: float
    turnover: float


@dataclass(frozen=True)
class PriceFeatures:
    average_turnover_20d: float | None
    volume_ratio_20d: float | None
    return_5d: float | None
    return_20d: float | None


def build_price_features(
    *,
    target_date: dt.date,
    today_close: float,
    today_volume: float,
    history: list[HistoricalPricePoint],
) -> PriceFeatures:
    """
    today_close / today_volume: TODAY's authoritative values — in
    production these come from the TWSE/TPEx daily-price mappers, not
    FinMind (FinMind's aggregation can lag behind the official
    exchange feeds on the same trading day).

    history: candidate historical sessions. Rows on or after
    target_date are defensively discarded here even if the caller
    included them by mistake.
    """
    valid_history = sorted(
        (
            point
            for point in history
            if point.trading_date < target_date
            and point.close > 0
            and point.volume >= 0
            and point.turnover >= 0
        ),
        key=lambda point: point.trading_date,
    )

    return PriceFeatures(
        average_turnover_20d=_trailing_average(valid_history, attr="turnover"),
        volume_ratio_20d=_volume_ratio(valid_history, today_volume=today_volume),
        return_5d=_compute_return(valid_history, today_close=today_close, days_back=5),
        return_20d=_compute_return(
            valid_history, today_close=today_close, days_back=TRAILING_WINDOW
        ),
    )


def _trailing_average(
    valid_history: list[HistoricalPricePoint], *, attr: str
) -> float | None:
    if len(valid_history) < TRAILING_WINDOW:
        return None
    trailing = valid_history[-TRAILING_WINDOW:]
    return sum(getattr(point, attr) for point in trailing) / TRAILING_WINDOW


def _volume_ratio(
    valid_history: list[HistoricalPricePoint], *, today_volume: float
) -> float | None:
    average_volume_20d = _trailing_average(valid_history, attr="volume")
    if average_volume_20d is None or average_volume_20d <= 0:
        return None
    return today_volume / average_volume_20d


def _compute_return(
    valid_history: list[HistoricalPricePoint], *, today_close: float, days_back: int
) -> float | None:
    if len(valid_history) < days_back:
        return None
    reference_close = valid_history[-days_back].close
    if reference_close <= 0:
        return None
    return today_close / reference_close - 1.0
