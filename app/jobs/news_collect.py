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
the other from being stored.  Exit code 1 if either source failed, so the
workflow run is marked failed and is visible; 0 otherwise.  Needs
DATABASE_URL.  Does not touch daily_ranking or LINE.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.db.news_models import ensure_news_tables
from app.db.news_repository import NewsRepository
from app.ingestion.announcement_client import AnnouncementClient
from app.ingestion.announcement_mapper import build_announcement_articles
from app.ingestion.market_data_client import RawSourcePayload, new_ingestion_run_id
from app.news.models import TAIPEI_TZ

logger = logging.getLogger(__name__)


class MemoryRepository:
    """Raw snapshots are not persisted by this job (same as the other
    ingestion jobs today); only the mapped articles are stored."""

    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def run(client: AnnouncementClient, engine: Engine, now: dt.datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(TAIPEI_TZ)  # run id, target date, fetched_at: one clock
    ensure_news_tables(engine)
    run_id, today, rc = new_ingestion_run_id(now), now.date(), 0
    for market, fetch in (("TWSE", client.fetch_twse), ("TPEX", client.fetch_tpex)):
        try:
            payload = fetch(ingestion_run_id=run_id, target_date=today)
            result = build_announcement_articles(
                payload.raw_payload, market=market, fetched_at=now
            )
            with Session(engine) as session:
                inserted = NewsRepository(session).save_new(result.articles)
        except Exception:
            logger.exception("news_collect_failed market=%s", market)
            rc = 1
            continue
        logger.info(
            "news_collect_ok market=%s mapped=%d dropped=%d inserted=%d",
            market,
            len(result.articles),
            result.dropped,
            inserted,
        )
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
