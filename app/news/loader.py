"""
Storage-backed news snapshot for one report day.

    snapshot = load_news_snapshot(session, stock_ids, report_date=..., ...)

What it adds to `select_news` (pure logic) is the question "can the stored
data be trusted for this report at all?", answered from the collection-run
records (app.news.runs):

  * For each market it takes the latest successful run that FINISHED by the
    17:00 cutoff; that run's start is the market's snapshot horizon, and only
    rows with fetched_at <= horizon are used.  A run that straddles the
    cutoff (16:59 -> 17:01) is therefore never half-visible.
  * A run's `finished_at` is read from the clock just BEFORE its record is
    committed, so for a moment a run with finished_at <= cutoff can have no
    visible record yet.  A load in that moment would use an older horizon
    and a later one a newer horizon.  To keep a snapshot from changing after
    its cutoff, the caller passes `now` and a load earlier than
    cutoff + RECORD_COMMIT_GRACE is DATA_UNAVAILABLE.  The grace is an
    assumption about how long one tiny INSERT can take, not a lock; the
    evening report starts well after it.
  * If a market has no such run, or its latest one started before
    `min_run_start` on the report day, every stock is DATA_UNAVAILABLE.
    The floors are per market and encode what the collector showed: the
    previous day's TWSE notices first appear in the 09:17 run, TPEx's in the
    15:17 run, so a TPEx run list whose 15:17 run failed would otherwise say
    "no suitable event" about notices it never received, while a failed TWSE
    15:17 run loses nothing.  They are parameters, not laws of the
    exchanges.

Limits (V1): all-or-nothing across markets, because a stock's market is not
known for stocks without any stored notice; and it relies on collection runs
never overlapping (the news-collect workflow's concurrency group).
"""

from __future__ import annotations

import datetime as dt
from typing import Callable, Iterable, Mapping

from sqlalchemy.orm import Session

from app.db.news_repository import CollectionRunRepository, NewsRepository
from app.news.models import TAIPEI_TZ
from app.news.select import Tier
from app.news.service import (
    NewsResult,
    NewsSnapshot,
    NewsStatus,
    check_window_days,
    history_start,
    select_news,
    snapshot_fetch_cutoff,
)

# How long after the cutoff a run record may still be waiting to commit.
RECORD_COMMIT_GRACE = dt.timedelta(minutes=5)

MIN_RUN_START_BY_MARKET: dict[str, dt.time] = {
    "TWSE": dt.time(9, 0),
    "TPEX": dt.time(15, 0),
}


def _unavailable(
    wanted: set[str], report_date: dt.date, cutoff: dt.datetime, reason: str
) -> NewsSnapshot:
    results = {
        s: NewsResult(s, NewsStatus.DATA_UNAVAILABLE, None, None) for s in wanted
    }
    return NewsSnapshot(results, report_date, cutoff, None, reason)


def load_news_snapshot(
    session: Session,
    stock_ids: Iterable[str],
    *,
    report_date: dt.date,
    now: dt.datetime,
    is_trading_day: Callable[[dt.date], bool],
    min_run_start: Mapping[str, dt.time] = MIN_RUN_START_BY_MARKET,
    min_tier: Tier = Tier.MEDIUM,
    base_days: int = 3,
    max_days: int = 5,
) -> NewsSnapshot:
    check_window_days(base_days, max_days)  # before any query or early return
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    missing = MIN_RUN_START_BY_MARKET.keys() - min_run_start.keys()
    if missing:  # an omitted market would silently skip its availability check
        raise ValueError(f"min_run_start must cover {sorted(missing)}")
    wanted = set(stock_ids)
    cutoff = snapshot_fetch_cutoff(report_date)
    if not wanted:  # nothing to ask about: no query, no availability verdict
        return NewsSnapshot({}, report_date, cutoff, None)
    if now < cutoff + RECORD_COMMIT_GRACE:
        return _unavailable(
            wanted,
            report_date,
            cutoff,
            f"too early: runs finished by {cutoff:%H:%M} may not be recorded"
            f" before {cutoff + RECORD_COMMIT_GRACE:%H:%M}",
        )

    runs = CollectionRunRepository(session)
    horizons: dict[str, dt.datetime] = {}
    for market, floor_time in min_run_start.items():
        floor = dt.datetime.combine(report_date, floor_time, tzinfo=TAIPEI_TZ)
        horizon = runs.snapshot_horizon(cutoff, market=market)
        if horizon is None:
            return _unavailable(
                wanted,
                report_date,
                cutoff,
                f"no successful {market} collection finished by {cutoff:%Y-%m-%d %H:%M}",
            )
        if horizon < floor:
            return _unavailable(
                wanted,
                report_date,
                cutoff,
                f"latest successful {market} run started {horizon:%Y-%m-%d %H:%M},"
                f" before {floor:%Y-%m-%d %H:%M}",
            )
        horizons[market] = horizon

    articles = NewsRepository(session).load_window(
        wanted,
        start=history_start(report_date, is_trading_day, max_days=max_days),
        end=report_date - dt.timedelta(days=1),
    )
    usable = [
        a
        for a in articles
        if a.market in horizons and a.fetched_at <= horizons[a.market]
    ]
    return select_news(
        usable,
        wanted,
        report_date=report_date,
        is_trading_day=is_trading_day,
        fetched_before=cutoff,
        min_tier=min_tier,
        base_days=base_days,
        max_days=max_days,
    )
