"""Pick the "recent focus" announcement for each stock of one report.

Pure logic: no database, no LINE.  The caller loads RawArticle rows and
hands them in, so a given input always gives the same output.

Rules (V1):
  * The report for day T can only contain announcements the source had
    already published for earlier days (announcement day D appears on D+1),
    so the window is the last `base_days` trading days BEFORE T, widened
    per stock to `max_days` only when it has nothing suitable.
  * Only notices of at least `min_tier` (default MEDIUM) count; a stock
    whose best notice is LOW has no suitable event.
  * Only rows first fetched at or before a FIXED cutoff
    (`snapshot_fetch_cutoff`) are used, and only notices published before
    the report day.  This keeps a later collection run or back-fill out of
    the result, but only if every row with fetched_at <= cutoff is already
    committed when the report is first built: fetched_at is the START time
    of a collection run, not the moment its rows became visible, so a run
    that starts at 16:59 and commits at 17:01 would be missed by a build at
    17:01 and admitted by a rebuild.  The storage-backed caller must close
    that gap (e.g. require the collection run to have finished before the
    cutoff, or persist the selection on the first build); this module does
    not and cannot.  Delivery refuses changed content under the same
    message version, so the gap matters.
  * A notice the source keeps re-publishing is judged by the EARLIEST copy
    in the rows supplied.  `history_start()` is a bounded look-back
    (default 90 days before the widest window): a notice first announced
    earlier than that, with no copy in between, looks new.  A caller that
    must recognise older re-publications has to supply the earlier copy.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from app.news.models import TAIPEI_TZ, RawArticle
from app.news.preprocess import collapse_repeats, trading_days_window_start
from app.news.select import Candidate, Tier, pick_representatives

SNAPSHOT_CUTOFF_TIME = dt.time(17, 0)
HISTORY_DAYS = 90
# trading_days_window_start looks back at most 366 calendar days; 60 trading
# days (about 85 calendar days) is far inside that for any real calendar.
MAX_SUPPORTED_DAYS = 60


class NewsStatus(str, Enum):
    HAS_EVENT = "HAS_EVENT"
    NO_SUITABLE_EVENT = "NO_SUITABLE_EVENT"


@dataclass(frozen=True)
class NewsResult:
    stock_id: str
    status: NewsStatus
    candidate: Candidate | None
    window_trading_days: int | None  # 3 or 5 when HAS_EVENT, else None


@dataclass(frozen=True)
class NewsSnapshot:
    results: dict[str, NewsResult]
    report_date: dt.date
    fetched_before: dt.datetime
    # Newest published date among the rows used (Taipei). It is the newest
    # announcement we hold, NOT proof that the source is complete up to it.
    latest_published_date: dt.date | None


def snapshot_fetch_cutoff(report_date: dt.date) -> dt.datetime:
    """17:00 Taipei on the report date: after the 09:17 (TWSE) and 15:17
    (TPEx) collection runs that carry the previous day's announcements,
    before the evening report starts and before the 21:17 run."""
    return dt.datetime.combine(report_date, SNAPSHOT_CUTOFF_TIME, tzinfo=TAIPEI_TZ)


def previous_trading_day(
    day: dt.date, is_trading_day: Callable[[dt.date], bool]
) -> dt.date:
    current = day
    for _ in range(366):
        current -= dt.timedelta(days=1)
        if is_trading_day(current):
            return current
    raise RuntimeError("no trading day found within a year")


def history_start(
    report_date: dt.date,
    is_trading_day: Callable[[dt.date], bool],
    *,
    max_days: int = 5,
    history_days: int = HISTORY_DAYS,
) -> dt.date:
    """Earliest published date the caller should load (a bounded look-back:
    `history_days` before the widest window), so most notices that began
    before the window are recognised as re-publications.  One whose
    earlier copies are all older than this is not."""
    last_day = previous_trading_day(report_date, is_trading_day)
    window_start = trading_days_window_start(last_day, max_days, is_trading_day)
    return window_start - dt.timedelta(days=history_days)


def select_news(
    articles: Iterable[RawArticle],
    stock_ids: Iterable[str],
    *,
    report_date: dt.date,
    is_trading_day: Callable[[dt.date], bool],
    fetched_before: dt.datetime | None = None,
    min_tier: Tier = Tier.MEDIUM,
    base_days: int = 3,
    max_days: int = 5,
) -> NewsSnapshot:
    if not 1 <= base_days <= max_days <= MAX_SUPPORTED_DAYS:
        raise ValueError(f"need 1 <= base_days <= max_days <= {MAX_SUPPORTED_DAYS}")
    cutoff = fetched_before or snapshot_fetch_cutoff(report_date)
    if cutoff.tzinfo is None:
        raise ValueError("fetched_before must be timezone-aware")
    wanted = set(stock_ids)

    # The report day itself is never part of the window: nothing published
    # on it may be used, even if it was fetched before the cutoff.  A Saturday
    # notice in a Monday report is still allowed.
    report_day_start = dt.datetime.combine(report_date, dt.time.min, tzinfo=TAIPEI_TZ)
    usable = [
        a
        for a in articles
        if a.stock_id in wanted
        and a.fetched_at <= cutoff
        and a.published_at < report_day_start
    ]
    latest = max(
        (a.published_at.astimezone(TAIPEI_TZ).date() for a in usable), default=None
    )
    notices = collapse_repeats(usable)

    last_day = previous_trading_day(report_date, is_trading_day)
    results: dict[str, NewsResult] = {}
    for days in sorted({base_days, max_days}):
        if wanted <= results.keys():
            break  # every stock is resolved: do not build the wider window
        start = trading_days_window_start(last_day, days, is_trading_day)
        since = dt.datetime.combine(start, dt.time.min, tzinfo=TAIPEI_TZ)
        for stock, cand in pick_representatives(notices, since=since).items():
            if cand.tier >= min_tier and stock not in results:
                results[stock] = NewsResult(stock, NewsStatus.HAS_EVENT, cand, days)
    for stock in wanted - results.keys():
        results[stock] = NewsResult(stock, NewsStatus.NO_SUITABLE_EVENT, None, None)
    return NewsSnapshot(results, report_date, cutoff, latest)
