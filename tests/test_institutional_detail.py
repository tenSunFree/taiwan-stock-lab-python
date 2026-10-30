import datetime as dt

import pytest

from app.domain.institutional_detail import (
    InstitutionalCategoryPoint,
    build_institutional_detail,
    format_lots,
    shares_to_lots,
)
from app.ingestion.finmind_mapper import build_institutional_category_points

TARGET = dt.date(2026, 10, 21)
# 11 consecutive weekday-ish dates before TARGET (calendar is irrelevant here)
DATES = [dt.date(2026, 10, 1) + dt.timedelta(days=i) for i in range(11)]


def point(day, foreign=0, trust=0, dealer=0):
    return InstitutionalCategoryPoint(
        trading_date=day,
        foreign_net_shares=foreign,
        investment_trust_net_shares=trust,
        dealer_net_shares=dealer,
    )


def volumes(dates=DATES):
    return {d: 100_000.0 for d in dates}


def full_points(**kw):
    return [point(d, **kw) for d in DATES]


def build(points, vols=None, **kw):
    return build_institutional_detail(
        target_date=TARGET,
        category_points=points,
        volume_by_date=volumes() if vols is None else vols,
        **kw,
    )


def test_uses_last_ten_sessions_before_target():
    detail = build(full_points(foreign=1000))
    assert len(detail.days) == 10
    assert detail.start_date == DATES[1]
    assert detail.as_of_date == DATES[-1]


def test_never_uses_target_date_or_later():
    points = full_points(foreign=1000) + [point(TARGET, foreign=9_999_000)]
    vols = {**volumes(), TARGET: 100_000.0}
    detail = build(points, vols)
    assert detail.as_of_date == DATES[-1]
    assert detail.category_total("foreign") == 10


def test_none_when_any_session_has_no_point_and_no_zero_fill():
    points = [p for p in full_points(foreign=1000) if p.trading_date != DATES[5]]
    assert build(points) is None


def test_none_when_fewer_than_ten_sessions_of_volume():
    assert build(full_points(), vols=volumes(DATES[-9:])) is None


def test_none_on_duplicate_date():
    assert build(full_points() + [point(DATES[3])]) is None


def test_totals_and_grand_total():
    points = [point(d, foreign=2000, trust=1000, dealer=-1000) for d in DATES]
    detail = build(points)
    assert detail.category_total("foreign") == 20
    assert detail.category_total("trust") == 10
    assert detail.category_total("dealer") == -10
    assert detail.grand_total == 20
    assert detail.grand_total == sum(d.total for d in detail.days)


def test_buy_sell_flat_day_counts_use_daily_total():
    totals = [5, 5, 5, -3, -3, 0, 5, 5, 5, 5]
    points = [point(d, foreign=t * 1000) for d, t in zip(DATES[1:], totals)]
    detail = build(points + [point(DATES[0])])
    assert (detail.total_buy_days, detail.total_sell_days, detail.total_flat_days) == (
        7,
        2,
        1,
    )


def test_streak_counts_back_from_newest_and_zero_breaks_it():
    values = [-1, 3, 3, 0, 2, 2, 2, 2, 2, 2]
    points = [point(d, foreign=v * 1000) for d, v in zip(DATES[1:], values)]
    detail = build(points + [point(DATES[0])])
    streak = detail.streak("foreign")
    assert (streak.direction, streak.days, streak.label) == ("buy", 6, "連6買")


def test_streak_none_when_newest_is_zero():
    values = [1] * 9 + [0]
    points = [point(d, trust=v * 1000) for d, v in zip(DATES[1:], values)]
    assert build(points + [point(DATES[0])]).streak("trust") is None


def test_sell_streak_label():
    values = [1] * 8 + [-1, -1]
    points = [point(d, dealer=v * 1000) for d, v in zip(DATES[1:], values)]
    assert build(points + [point(DATES[0])]).streak("dealer").label == "連2賣"


def test_lots_rounding_is_half_away_from_zero():
    assert shares_to_lots(1500) == 2
    assert shares_to_lots(-1500) == -2
    assert shares_to_lots(499) == 0
    assert shares_to_lots(-500) == -1
    assert shares_to_lots(0) == 0


def test_sub_half_lot_cell_counts_as_zero_everywhere():
    # 400 shares rounds to 0 lots: that cell must not extend a streak or
    # count as a buy day, because the table prints "0".
    points = [point(d, foreign=400) for d in DATES[1:]] + [point(DATES[0])]
    detail = build(points)
    assert detail.category_total("foreign") == 0
    assert detail.total_buy_days == 0
    assert detail.streak("foreign") is None


def test_format_lots():
    assert format_lots(12450) == "+12,450"
    assert format_lots(-860) == "-860"
    assert format_lots(0) == "0"
    assert format_lots(14710, signed=False) == "14,710"


def _day(n):
    from app.domain.institutional_detail import InstitutionalDayLots

    return InstitutionalDayLots(DATES[0] + dt.timedelta(days=n), 1, 1, 1)


@pytest.mark.parametrize("count", [0, 8, 9, 11])
def test_detail_requires_exactly_ten_sessions(count):
    from app.domain.institutional_detail import InstitutionalDetail10D

    with pytest.raises(ValueError, match="exactly 10"):
        InstitutionalDetail10D(days=tuple(_day(n) for n in range(count)))


def test_detail_rejects_duplicate_or_unordered_dates():
    from app.domain.institutional_detail import InstitutionalDetail10D

    days = [_day(n) for n in range(10)]
    with pytest.raises(ValueError, match="strictly increasing"):
        InstitutionalDetail10D(days=tuple(days[:5] + [days[4]] + days[6:]))
    with pytest.raises(ValueError, match="strictly increasing"):
        InstitutionalDetail10D(days=tuple(reversed(days)))
    InstitutionalDetail10D(days=tuple(days))  # a valid one still builds


# --- mapper ------------------------------------------------------------------

NAMES = [
    "Foreign_Investor",
    "Foreign_Dealer_Self",
    "Investment_Trust",
    "Dealer_self",
    "Dealer_Hedging",
]


def rows_for(day, *, skip=(), extra=()):
    rows = []
    for index, name in enumerate(NAMES):
        if name in skip:
            continue
        rows.append(
            {
                "date": day.isoformat(),
                "stock_id": "2330",
                "name": name,
                "buy": 1000 * (index + 2),
                "sell": 1000 * index,
            }
        )
    rows.extend(extra)
    return rows


def test_mapper_groups_five_names_into_three_categories():
    [p] = build_institutional_category_points(
        rows_for(DATES[0]), expected_stock_id="2330"
    )
    # every name nets +2000 shares
    assert p.foreign_net_shares == 4000  # Foreign_Investor + Foreign_Dealer_Self
    assert p.investment_trust_net_shares == 2000
    assert p.dealer_net_shares == 4000  # Dealer_self + Dealer_Hedging


def test_mapper_total_equals_existing_aggregate():
    from app.ingestion.finmind_mapper import build_institutional_flow_points

    rows = rows_for(DATES[0]) + rows_for(DATES[1])
    cats = build_institutional_category_points(rows, expected_stock_id="2330")
    agg = build_institutional_flow_points(rows, expected_stock_id="2330")
    assert [
        p.foreign_net_shares + p.investment_trust_net_shares + p.dealer_net_shares
        for p in cats
    ] == [p.net_shares for p in agg]


def test_mapper_drops_date_with_missing_required_name():
    rows = rows_for(DATES[0], skip=("Dealer_Hedging",)) + rows_for(DATES[1])
    points = build_institutional_category_points(rows, expected_stock_id="2330")
    assert [p.trading_date for p in points] == [DATES[1]]


def test_mapper_drops_date_with_unknown_name():
    bad = {
        "date": DATES[0].isoformat(),
        "stock_id": "2330",
        "name": "Mystery_Fund",
        "buy": 1,
        "sell": 0,
    }
    rows = rows_for(DATES[0], extra=[bad]) + rows_for(DATES[1])
    points = build_institutional_category_points(rows, expected_stock_id="2330")
    assert [p.trading_date for p in points] == [DATES[1]]


def test_mapper_drops_date_with_invalid_numbers_or_duplicates():
    bad_number = rows_for(DATES[0])
    bad_number[2]["buy"] = "oops"
    duplicate = rows_for(DATES[1]) + rows_for(DATES[1])[:1]
    ok = rows_for(DATES[2])
    points = build_institutional_category_points(
        bad_number + duplicate + ok, expected_stock_id="2330"
    )
    assert [p.trading_date for p in points] == [DATES[2]]


def test_mapper_ignores_other_stock_and_rejects_blank_id():
    other = [dict(r, stock_id="9999") for r in rows_for(DATES[0])]
    assert build_institutional_category_points(other, expected_stock_id="2330") == []
    with pytest.raises(ValueError):
        build_institutional_category_points([], expected_stock_id=" ")
