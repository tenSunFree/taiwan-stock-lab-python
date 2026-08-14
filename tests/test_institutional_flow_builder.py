import datetime as dt

import pytest

from app.domain.institutional_flow_builder import (
    InstitutionalFlowPoint,
    build_institutional_net_buy_ratio,
)

TARGET_DATE = dt.date(2026, 8, 13)


def make_points(
    count: int = 5, *, start: dt.date = dt.date(2026, 8, 1), net_shares: int = 1000
):
    return [
        InstitutionalFlowPoint(
            trading_date=start + dt.timedelta(days=i), net_shares=net_shares
        )
        for i in range(count)
    ]


def make_volume_map(points, *, volume: float = 100_000.0):
    return {point.trading_date: volume for point in points}


def test_ratio_computed_with_full_window():
    points = make_points(5, net_shares=1000)
    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE,
        flow_points=points,
        volume_by_date=make_volume_map(points, volume=100_000.0),
    )
    # (1000*5) / (100000*5) = 5000/500000 = 0.01
    assert ratio == pytest.approx(0.01)


def test_none_when_window_incomplete():
    points = make_points(4)
    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE,
        flow_points=points,
        volume_by_date=make_volume_map(points),
    )
    assert ratio is None


def test_none_when_a_day_in_window_is_missing_volume():
    points = make_points(5)
    volume_map = make_volume_map(points)
    del volume_map[points[2].trading_date]
    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE, flow_points=points, volume_by_date=volume_map
    )
    assert ratio is None


def test_target_date_row_is_defensively_excluded():
    points = make_points(5, net_shares=1000)
    points.append(InstitutionalFlowPoint(trading_date=TARGET_DATE, net_shares=999_999))
    volume_map = make_volume_map(points)
    volume_map[TARGET_DATE] = 100_000.0
    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE, flow_points=points, volume_by_date=volume_map
    )
    assert ratio == pytest.approx(0.01)


def test_negative_net_buy_produces_negative_ratio():
    points = make_points(net_shares=-500)
    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE,
        flow_points=points,
        volume_by_date=make_volume_map(points, volume=100_000.0),
    )
    # (-500*5) / (100000*5) = -2500/500000 = -0.005
    assert ratio == pytest.approx(-0.005)


def test_latest_five_sessions_are_used():
    old_point = InstitutionalFlowPoint(
        trading_date=dt.date(2026, 7, 31), net_shares=999_999
    )
    latest = make_points(count=5, net_shares=1000)
    points = [old_point, *latest]
    volume_by_date = {
        old_point.trading_date: 100_000.0,
        **make_volume_map(latest, volume=100_000.0),
    }

    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE, flow_points=points, volume_by_date=volume_by_date
    )
    assert ratio == pytest.approx(0.01)


def test_rejects_non_positive_window():
    with pytest.raises(ValueError, match="window must be positive"):
        build_institutional_net_buy_ratio(
            target_date=TARGET_DATE, flow_points=[], volume_by_date={}, window=0
        )


def test_missing_recent_flow_session_does_not_fall_back_to_older_flow():
    price_dates = [
        dt.date(2026, 8, 6),
        dt.date(2026, 8, 7),
        dt.date(2026, 8, 10),
        dt.date(2026, 8, 11),
        dt.date(2026, 8, 12),
    ]

    flow_points = [
        # Older row that must NOT be used as a substitute.
        InstitutionalFlowPoint(
            trading_date=dt.date(
                2026,
                8,
                5,
            ),
            net_shares=999_999,
        ),
        InstitutionalFlowPoint(
            trading_date=dt.date(
                2026,
                8,
                6,
            ),
            net_shares=1_000,
        ),
        InstitutionalFlowPoint(
            trading_date=dt.date(
                2026,
                8,
                7,
            ),
            net_shares=1_000,
        ),
        # 2026-08-10 intentionally missing.
        InstitutionalFlowPoint(
            trading_date=dt.date(
                2026,
                8,
                11,
            ),
            net_shares=1_000,
        ),
        InstitutionalFlowPoint(
            trading_date=dt.date(
                2026,
                8,
                12,
            ),
            net_shares=1_000,
        ),
    ]

    volume_by_date = {trading_date: 100_000.0 for trading_date in price_dates}

    ratio = build_institutional_net_buy_ratio(
        target_date=TARGET_DATE,
        flow_points=flow_points,
        volume_by_date=volume_by_date,
    )

    assert ratio is None
