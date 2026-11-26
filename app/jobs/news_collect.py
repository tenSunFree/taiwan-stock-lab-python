"""
Daily collection of official announcements (TWSE / TPEx 每日重大訊息) into
the raw_articles table.

    python -m app.jobs.news_collect

No assumption is made about how many historical days either endpoint
returns: each run stores whatever batch it sees, so a rolling multi-day
window builds up from repeated runs.  Safe to run repeatedly: rows are
keyed by article_id and an already-stored article is left untouched
(ON CONFLICT DO NOTHING), keeping its first-seen fetched_at.

TWSE and TPEx are handled independently: one source failing never blocks
the other from being stored.  Each source's attempt is also recorded in
news_collection_runs (started/finished time, success, row counts, published
date range; see app.news.runs).  The record is written AFTER the articles
are committed, so its finished_at means "every row of this run is visible".
Exit code 1 if either source failed, or if a record could not be written (a
missing record makes that run unusable as a snapshot), so the workflow run
is marked failed and is visible; 0 otherwise.  Needs DATABASE_URL.  Does not
touch daily_ranking or LINE.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from collections import Counter
from collections.abc import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.news_models import ensure_news_tables
from app.db.news_repository import CollectionRunRepository, NewsRepository
from app.ingestion.announcement_client import AnnouncementClient
from app.ingestion.announcement_mapper import (
    build_announcement_articles,
    parse_date,
)
from app.ingestion.market_data_client import RawSourcePayload, new_ingestion_run_id
from app.news.models import TAIPEI_TZ
from app.news.runs import CollectionRun

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Raw snapshots are not persisted by this job (same as the other
    ingestion jobs today); only the mapped articles are stored."""

    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


REPORT_DATE_KEYS = ("出表日期", "Date")  # TWSE / TPEx name for the dataset date


def _format_counts(counts: Counter) -> str:
    return ",".join(f"{d.isoformat()}:{n}" for d, n in sorted(counts.items())) or "-"


def _report_date_counts(rows: object) -> Counter:
    """Distribution of the dataset's own 出表日期 (TPEx: Date). Logged only,
    to learn how fresh each endpoint's batch is; never used for storage."""
    counts: Counter = Counter()
    if not isinstance(rows, list):
        return counts
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            if str(key).replace("\ufeff", "").strip() in REPORT_DATE_KEYS:
                d = parse_date(str(value))
                if d is not None:
                    counts[d] += 1
                break
    return counts


def _record_run(engine: Engine, record: CollectionRun) -> bool:
    try:
        with Session(engine) as session:
            CollectionRunRepository(session).record(record)
    except Exception:
        logger.exception("news_collect_run_record_failed market=%s", record.market)
        return False
    return True


def run(
    client: AnnouncementClient,
    engine: Engine,
    now: dt.datetime,
    *,
    clock: Callable[[], dt.datetime] | None = None,
) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(TAIPEI_TZ)  # run id, target date, fetched_at: one clock
    clock = clock or (lambda: dt.datetime.now(TAIPEI_TZ))
    ensure_news_tables(engine)
    run_id, today, rc = new_ingestion_run_id(now), now.date(), 0
    for market, fetch in (("TWSE", client.fetch_twse), ("TPEX", client.fetch_tpex)):
        raw = inserted = None
        published: Counter = Counter()
        error = None
        try:
            payload = fetch(ingestion_run_id=run_id, target_date=today)
            result = build_announcement_articles(
                payload.raw_payload, market=market, fetched_at=now
            )
            raw = (
                len(payload.raw_payload) if isinstance(payload.raw_payload, list) else 0
            )
            with Session(engine) as session:
                inserted = NewsRepository(session).save_new(result.articles)
            published = Counter(
                a.published_at.astimezone(TAIPEI_TZ).date() for a in result.articles
            )
        except Exception as exc:
            logger.exception("news_collect_failed market=%s", market)
            rc = 1
            error = type(exc).__name__
        else:
            logger.info(
                "news_collect_ok market=%s raw=%d mapped=%d dropped=%d inserted=%d "
                "published_dates=%s report_dates=%s",
                market,
                raw,
                len(result.articles),
                result.dropped,
                inserted,
                _format_counts(published),
                _format_counts(_report_date_counts(payload.raw_payload)),
            )
        # Taken only after the articles above are committed (or the attempt
        # failed), never before.  max() guards a clock that runs behind `now`.
        finished = max(clock().astimezone(TAIPEI_TZ), now)
        record = CollectionRun(
            run_id=run_id,
            market=market,
            started_at=now,
            finished_at=finished,
            success=error is None,
            raw_rows=raw,
            mapped_rows=None if error else len(result.articles),
            inserted_rows=inserted,
            first_published_date=min(published) if published else None,
            last_published_date=max(published) if published else None,
            error=error,
        )
        if not _record_run(engine, record):
            rc = 1
    return rc


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return 1
    engine = create_engine(database_url)
    client = AnnouncementClient(MemoryRepository())
    try:
        return run(client, engine, dt.datetime.now(TAIPEI_TZ))
    finally:
        client.http_client.close()
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
