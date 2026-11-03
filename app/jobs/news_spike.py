"""
Manual source check for the news module. Run by hand to verify the
TWSE/TPEx announcement endpoints and columns against real data:

    python -m app.jobs.news_spike

Goes through AnnouncementClient (retry + raw snapshot) and the mapper for
TWSE and TPEx independently; prints status, row count, REAL column names,
mapped count, dropped count and a few samples.  No DB, no LLM.
Exit code 1 if either source fails.
"""

from __future__ import annotations

import datetime as dt
import sys

from app.ingestion.announcement_client import AnnouncementClient
from app.ingestion.announcement_mapper import build_announcement_articles
from app.ingestion.market_data_client import RawSourcePayload, new_ingestion_run_id
from app.news.models import TAIPEI_TZ


class MemoryRepository:
    def __init__(self) -> None:
        self.saved: list[RawSourcePayload] = []

    def save(self, payload: RawSourcePayload) -> None:
        self.saved.append(payload)


def run(client: AnnouncementClient, now: dt.datetime) -> int:
    run_id, today, rc = new_ingestion_run_id(), now.date(), 0
    for market, fetch in (("TWSE", client.fetch_twse), ("TPEX", client.fetch_tpex)):
        print(f"\n===== {market} =====")
        try:
            payload = fetch(ingestion_run_id=run_id, target_date=today)
        except Exception as exc:
            print("FETCH FAILED:", repr(exc))
            rc = 1
            continue
        rows = payload.raw_payload
        print("rows:", len(rows) if isinstance(rows, list) else type(rows).__name__)
        first_row = (
            next((r for r in rows if isinstance(r, dict)), None)
            if isinstance(rows, list)
            else None
        )
        if first_row is not None:
            print("columns:", list(first_row.keys()))
        try:
            result = build_announcement_articles(rows, market=market, fetched_at=now)
        except Exception as exc:
            print("MAPPER FAILED:", repr(exc))
            rc = 1
            continue
        print(f"mapped: {len(result.articles)}  dropped: {result.dropped}")
        for a in result.articles[:3]:
            print(
                f"  {a.stock_id} {a.published_at.isoformat()} "
                f"{a.session.value} {a.title[:40]}"
            )
    return rc


def main() -> int:
    return run(AnnouncementClient(MemoryRepository()), dt.datetime.now(TAIPEI_TZ))


if __name__ == "__main__":
    sys.exit(main())
