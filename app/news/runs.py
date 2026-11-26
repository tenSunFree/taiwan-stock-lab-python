"""
One record per source per collection run (pure dataclass, no I/O).

Why it exists: raw_articles.fetched_at is the START time of a collection run
(`news_collect.run` sets it once, before it fetches), while the rows only
become visible when that run commits.  Nothing in raw_articles says when a
run finished or whether a source failed, so a reader cannot tell a quiet day
from a failed collection, nor build a snapshot that later runs cannot
change.  A CollectionRun supplies that:

  * `started_at` equals the `fetched_at` of every row that run inserted
    (both markets of one job share the job's start time);
  * `finished_at` is taken AFTER the articles were committed, so
    "finished_at <= cutoff" means every row of that run was already visible
    at the cutoff;
  * `success` is False when the source or its storage step failed.

Reader rule (the loader PR): for each market take the latest successful run
that finished by the cutoff; its `started_at` is the snapshot horizon, and a
row is usable when `fetched_at <= horizon`.  This does not depend on every
row being linked to a record: if a run committed its rows but crashed (or
failed to write) its record, those rows are admitted as soon as a LATER
successful run finishes by the cutoff, and stay out of an earlier cutoff
for good.  Either way the answer for a given cutoff never changes, because
runs of the news-collect workflow never overlap (its concurrency group
queues them), so nothing can commit with an older fetched_at after a later
run has finished.  If that serialisation is ever removed, this rule is no
longer safe.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionRun:
    run_id: str
    market: str
    started_at: dt.datetime
    finished_at: dt.datetime
    success: bool
    raw_rows: int | None = None  # rows the endpoint returned
    mapped_rows: int | None = None  # rows that mapped to articles
    inserted_rows: int | None = None  # rows actually new in raw_articles
    first_published_date: dt.date | None = None  # Taipei date range of the
    last_published_date: dt.date | None = None  # mapped articles
    error: str | None = None  # exception class name only

    def __post_init__(self) -> None:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("started_at and finished_at must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        if self.success and self.error is not None:
            raise ValueError("a successful run has no error")
