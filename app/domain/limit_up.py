"""
Limit-up detection.

Decision precedence:
    1. Limit-up price provided directly by the data source (most
       reliable, e.g. TWSE's officially disclosed limit-up/limit-down
       price).
    2. Calculated from the opening reference price + tick rules
       (app.domain.price_ticks).
    3. Never use "previous close * 1.1" or "daily change >= 9.5%" as
       the formal determination — those are only acceptable for rough
       pre-screening / data-anomaly hints.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from app.domain.price_ticks import calculate_limit_up_price


class LimitUpSource(str, Enum):
    SOURCE_PROVIDED = "SOURCE_PROVIDED"
    CALCULATED = "CALCULATED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class LimitUpResult:
    is_close_limit_up: bool
    """Close price equals the limit-up price — the definition this
    system uses for stock selection."""

    has_touched_limit_up: bool
    """Intraday high equalled the limit-up price, even if the close
    did not stay locked there (i.e. the limit-up was "opened" during
    the session). Not used for selection, only for risk annotation /
    future factors such as "number of times reopened"."""

    limit_up_price: Decimal | None
    limit_up_source: LimitUpSource
    reason: str

    @property
    def is_limit_up(self) -> bool:
        """Backward-compatible alias: "limit-up" in this system always
        means close limit-up."""
        return self.is_close_limit_up


def resolve_limit_up_price(
    *,
    source_limit_up_price: Decimal | None,
    reference_price: Decimal | None,
    has_price_limit_today: bool,
) -> tuple[Decimal | None, LimitUpSource]:
    """Determine today's legal limit-up price for a stock; does not
    decide whether it actually closed at that price."""
    if not has_price_limit_today:
        # e.g. first day of listing, ex-rights/ex-dividend day, or
        # other special periods with no daily price limit
        return None, LimitUpSource.UNAVAILABLE

    if source_limit_up_price is not None:
        return source_limit_up_price, LimitUpSource.SOURCE_PROVIDED

    if reference_price is not None:
        return calculate_limit_up_price(reference_price), LimitUpSource.CALCULATED

    return None, LimitUpSource.UNAVAILABLE


def evaluate_limit_up(
    *,
    security_type: str,
    close_price: Decimal | None,
    high_price: Decimal | None = None,
    source_limit_up_price: Decimal | None,
    reference_price: Decimal | None,
    has_price_limit_today: bool,
    data_quality_ok: bool,
) -> LimitUpResult:
    """
    Full limit-up determination:
        common stock + daily price limit applies today + a valid
        limit-up price exists + close price equals it + data quality
        passed.

    high_price is optional: if provided, has_touched_limit_up is also
    computed (intraday touch that may not have held into the close);
    this is informational only and never affects selection.
    """
    if security_type != "COMMON_STOCK":
        return LimitUpResult(False, False, None, LimitUpSource.UNAVAILABLE, "not a common stock, rule does not apply")

    if not data_quality_ok:
        return LimitUpResult(False, False, None, LimitUpSource.UNAVAILABLE, "data quality check failed")

    if close_price is None:
        return LimitUpResult(False, False, None, LimitUpSource.UNAVAILABLE, "missing close price")

    limit_up_price, source = resolve_limit_up_price(
        source_limit_up_price=source_limit_up_price,
        reference_price=reference_price,
        has_price_limit_today=has_price_limit_today,
    )

    if limit_up_price is None:
        return LimitUpResult(
            False, False, None, source, "no valid limit-up price today (e.g. no price limit period)"
        )

    is_close_up = close_price == limit_up_price
    has_touched = (high_price is not None) and (high_price == limit_up_price)
    reason = "close price equals limit-up price" if is_close_up else "close price below limit-up price"
    return LimitUpResult(is_close_up, has_touched, limit_up_price, source, reason)
