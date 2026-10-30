"""
Pure, provider-independent 10-session institutional detail (DISPLAY-ONLY).

This is an ADDITIVE view next to app.domain.institutional_flow_builder:

- institutional_flow_builder keeps owning the 5-session net-buy RATIO
  (scoring factor) and the 3-session SIGN check. Nothing here touches
  them, FACTOR_WEIGHTS, or any scoring input.
- This module answers a different question for the report's 法人籌碼
  card and its 📊 明細 image: "what did 外資 / 投信 / 自營商 each do on
  each of the last 10 sessions?" — one table of 10 x 3 values that the
  summary, the chart and the table ALL read from (a single object, so
  the three can never disagree).

DESIGN DECISION — categories (fixed, see finmind_mapper):
    外資   = Foreign_Investor + Foreign_Dealer_Self
    投信   = Investment_Trust
    自營商 = Dealer_self (自行買賣) + Dealer_Hedging (避險)   [both]
    合計   = 外資 + 投信 + 自營商  (== the existing aggregate net_shares)

DESIGN DECISION — units: input points are SHARES (FinMind). This module
converts each cell to 張 (lots, 1 lot = 1000 shares) exactly once, with
half-away-from-zero rounding, and everything downstream (totals,
streaks, buy/sell day counts, chart, table) is computed from those
already-rounded integer lots. So the printed table always adds up.

DESIGN DECISION — window / no look-ahead / no fabrication: same rule as
institutional_flow_builder. The 10 sessions are the last 10 eligible
trading dates strictly before target_date, defined by the price-volume
calendar. If ANY of those 10 dates lacks a complete category point, the
result is None — never padded with 0, never filled from an older date.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

INSTITUTIONAL_DETAIL_WINDOW = 10
SHARES_PER_LOT = 1000

CATEGORY_FOREIGN = "foreign"
CATEGORY_TRUST = "trust"
CATEGORY_DEALER = "dealer"
CATEGORIES = (CATEGORY_FOREIGN, CATEGORY_TRUST, CATEGORY_DEALER)
CATEGORY_LABELS = {
    CATEGORY_FOREIGN: "外資",
    CATEGORY_TRUST: "投信",
    CATEGORY_DEALER: "自營商",
}


@dataclass(frozen=True)
class InstitutionalCategoryPoint:
    """One trading date, net shares per category (buy - sell). Produced by
    app.ingestion.finmind_mapper.build_institutional_category_points."""

    trading_date: dt.date
    foreign_net_shares: int
    investment_trust_net_shares: int
    dealer_net_shares: int


@dataclass(frozen=True)
class InstitutionalDayLots:
    trading_date: dt.date
    foreign: int
    trust: int
    dealer: int

    @property
    def total(self) -> int:
        return self.foreign + self.trust + self.dealer

    def value(self, category: str) -> int:
        return {
            CATEGORY_FOREIGN: self.foreign,
            CATEGORY_TRUST: self.trust,
            CATEGORY_DEALER: self.dealer,
        }[category]


@dataclass(frozen=True)
class InstitutionalStreak:
    direction: str  # "buy" | "sell"
    days: int

    @property
    def label(self) -> str:
        return f"連{self.days}{'買' if self.direction == 'buy' else '賣'}"


@dataclass(frozen=True)
class InstitutionalDetail10D:
    days: tuple[InstitutionalDayLots, ...]  # oldest -> newest, in lots

    def __post_init__(self) -> None:
        # Invariants: the chart, the table and the Flex summary all assume
        # exactly one row per session, oldest first. A half-built or
        # unordered object must fail HERE, not render a plausible lie.
        if len(self.days) != INSTITUTIONAL_DETAIL_WINDOW:
            raise ValueError(
                f"InstitutionalDetail10D needs exactly "
                f"{INSTITUTIONAL_DETAIL_WINDOW} sessions, got {len(self.days)}"
            )
        dates = [day.trading_date for day in self.days]
        if any(later <= earlier for earlier, later in zip(dates, dates[1:])):
            raise ValueError(
                "InstitutionalDetail10D sessions must be unique and strictly increasing"
            )

    @property
    def start_date(self) -> dt.date:
        return self.days[0].trading_date

    @property
    def as_of_date(self) -> dt.date:
        return self.days[-1].trading_date

    def category_total(self, category: str) -> int:
        return sum(day.value(category) for day in self.days)

    @property
    def grand_total(self) -> int:
        return sum(day.total for day in self.days)

    def streak(self, category: str) -> InstitutionalStreak | None:
        return _streak([day.value(category) for day in self.days])

    @property
    def total_buy_days(self) -> int:
        return sum(1 for day in self.days if day.total > 0)

    @property
    def total_sell_days(self) -> int:
        return sum(1 for day in self.days if day.total < 0)

    @property
    def total_flat_days(self) -> int:
        return sum(1 for day in self.days if day.total == 0)


def shares_to_lots(shares: int) -> int:
    """1 lot = 1000 shares; ties round away from zero (so -500 -> -1)."""
    return int(
        (Decimal(shares) / SHARES_PER_LOT).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def format_lots(lots: int, *, signed: bool = True) -> str:
    """+12,450 / -860 / 0  (zero is never printed with a sign)."""
    if lots == 0:
        return "0"
    return f"{lots:+,}" if signed else f"{lots:,}"


def _streak(values: Sequence[int]) -> InstitutionalStreak | None:
    """Trailing run ending at the newest value. A zero newest value, or a
    zero inside the run, breaks it (0 is neither buying nor selling)."""
    if not values or values[-1] == 0:
        return None
    buying = values[-1] > 0
    days = 0
    for value in reversed(values):
        if (value > 0) == buying and value != 0:
            days += 1
        else:
            break
    return InstitutionalStreak(direction="buy" if buying else "sell", days=days)


def build_institutional_detail(
    *,
    target_date: dt.date,
    category_points: list[InstitutionalCategoryPoint],
    volume_by_date: dict[dt.date, float],
) -> InstitutionalDetail10D | None:
    """The last 10 eligible sessions before target_date, or None when any
    of them has no complete category point (see module docstring)."""
    window = INSTITUTIONAL_DETAIL_WINDOW

    point_by_date: dict[dt.date, InstitutionalCategoryPoint] = {}
    for point in category_points:
        if point.trading_date >= target_date:
            continue
        # Mapper emits one point per date; a duplicate is invalid input.
        if point.trading_date in point_by_date:
            return None
        point_by_date[point.trading_date] = point

    eligible_dates = sorted(
        trading_date
        for trading_date, volume in volume_by_date.items()
        if trading_date < target_date and volume > 0
    )
    if len(eligible_dates) < window:
        return None

    days: list[InstitutionalDayLots] = []
    for trading_date in eligible_dates[-window:]:
        point = point_by_date.get(trading_date)
        if point is None:
            return None
        days.append(
            InstitutionalDayLots(
                trading_date=trading_date,
                foreign=shares_to_lots(point.foreign_net_shares),
                trust=shares_to_lots(point.investment_trust_net_shares),
                dealer=shares_to_lots(point.dealer_net_shares),
            )
        )
    return InstitutionalDetail10D(days=tuple(days))
