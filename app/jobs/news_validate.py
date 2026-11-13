"""
Read-only validation of the news pipeline on real stored announcements.

    python -m app.jobs.news_validate --start 2026-11-01 --end 2026-11-13 \
        [--out news_validation] [--sample 40] [--stocks 2330,2317]

Reads official TWSE/TPEx rows of raw_articles whose published_at falls in
the given TAIPEI dates (inclusive), then runs the SAME code the product
uses (collapse_repeats -> classify -> pick_representatives).  Writes into
--out:

  notices.csv  every collapsed notice with type, tier and whether it was
               picked
  samples.csv  random samples of 資產交易 and 其他, EVERY picked 其他 and every
               picked notice of --stocks (each notice listed once, with its
               groups), plus empty expected_type / review_note columns for
               a human to fill in
  report.md    counts and percentages, 其他 per day and market, what the
               picks look like, the most common unclassified titles

Nothing is written to the database.  Precision and missed-event counts
need the human columns: this job only prepares the sample.  Needs
DATABASE_URL.  Collapsing only sees the requested range (see
collapse_repeats), so a notice that began before --start looks new.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import logging
import os
import random
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.news_models import RawArticleRow
from app.db.news_repository import NewsRepository
from app.news.classify import EventType
from app.news.models import TAIPEI_TZ, RawArticle
from app.news.preprocess import RepeatedNotice, collapse_repeats
from app.news.select import Candidate, assess, pick_representatives

logger = logging.getLogger(__name__)

NOTICE_COLUMNS = [
    "article_id",
    "stock_id",
    "market",
    "title",
    "description",
    "first_published_taipei",
    "last_published_taipei",
    "first_fetched_taipei",
    "occurrences",
    "predicted_type",
    "subsidiary",
    "tier",
    "selected",
]
SAMPLE_COLUMNS = ["sample_groups", *NOTICE_COLUMNS, "expected_type", "review_note"]


@dataclass(frozen=True)
class Analysis:
    notices_rows: list[dict]
    sample_rows: list[dict]
    report: str


def _taipei(value: dt.datetime) -> str:
    return value.astimezone(TAIPEI_TZ).strftime("%Y-%m-%d %H:%M")


def _shape(title: str) -> str:
    """Group titles that differ only by numbers/ids, for the 其他 list."""
    return re.sub(r"\d+(?:\.\d+)?", "N", title)[:60]


def _pct(part: int, whole: int) -> str:
    return f"{part / whole:.1%}" if whole else "-"


def analyze(
    articles: list[RawArticle],
    *,
    sample: int = 40,
    seed: int = 20261113,
    stocks: frozenset[str] = frozenset(),
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> Analysis:
    official = [a for a in articles if a.is_official and a.market in {"TWSE", "TPEX"}]
    notices = collapse_repeats(official)
    picks = pick_representatives(notices)
    picked_ids = {c.notice.article.article_id for c in picks.values()}

    rows: list[tuple[dict, RepeatedNotice, Candidate | None]] = []
    for n in notices:
        cand = assess(n)
        a = n.article
        rows.append(
            (
                {
                    "article_id": a.article_id,
                    "stock_id": a.stock_id,
                    "market": a.market,
                    "title": a.title,
                    "description": a.description or "",
                    "first_published_taipei": _taipei(n.first_published_at),
                    "last_published_taipei": _taipei(n.last_published_at),
                    "first_fetched_taipei": _taipei(a.fetched_at),
                    "occurrences": n.occurrences,
                    "predicted_type": cand.event_type.value if cand else "股價異常公告",
                    "subsidiary": cand.is_subsidiary if cand else "",
                    "tier": cand.tier.name if cand else "EXCLUDED",
                    "selected": a.article_id in picked_ids,
                },
                n,
                cand,
            )
        )
    notices_rows = [r for r, _, _ in rows]

    rng = random.Random(seed)
    groups_by_id: dict[str, list[str]] = {}
    order: list[dict] = []

    def add_group(name: str, items: list[dict], cap: int | None) -> None:
        chosen = items if cap is None or len(items) <= cap else rng.sample(items, cap)
        for r in chosen:
            if r["article_id"] not in groups_by_id:
                groups_by_id[r["article_id"]] = []
                order.append(r)
            groups_by_id[r["article_id"]].append(name)

    by_type = lambda t: [
        r for r in notices_rows if r["predicted_type"] == t
    ]  # noqa: E731
    add_group("資產交易", by_type("資產交易"), sample)
    add_group("其他", by_type("其他"), sample)
    # Always reviewed in full: these are what the user would actually see.
    add_group("picked_other", [r for r in by_type("其他") if r["selected"]], None)
    add_group(
        "picked_stocks",
        [r for r in notices_rows if r["stock_id"] in stocks and r["selected"]],
        None,
    )
    # A notice in several groups is listed once, so it is reviewed once.
    sample_rows = [
        {
            "sample_groups": "|".join(groups_by_id[r["article_id"]]),
            **r,
            "expected_type": "",
            "review_note": "",
        }
        for r in order
    ]

    report = _report(rows, picks, len(official), len(notices), start, end)
    return Analysis(notices_rows, sample_rows, report)


def _report(rows, picks, n_official: int, n_notices: int, start, end) -> str:
    types = Counter(r["predicted_type"] for r, _, _ in rows)
    classified = [(r, n) for r, n, c in rows if r["tier"] != "EXCLUDED"]
    other = [(r, n) for r, n in classified if r["predicted_type"] == "其他"]
    span = f"{start} to {end} (Taipei, inclusive)" if start and end else "not given"
    eligible = sum(r["tier"] != "EXCLUDED" for r, _, _ in rows)
    lines = [
        "# News validation report",
        "",
        f"- range: {span}",
        "- Range-local: a notice that began before the range looks new, and "
        "`selected` is the best notice per stock over the WHOLE range. It is "
        "NOT what the daily report would show with a 3/5 trading-day window.",
        "",
        f"- official rows: {n_official}",
        f"- notices after collapse_repeats: {n_notices} "
        f"({n_official - n_notices} repeats merged)",
        f"- eligible for selection (not 股價異常公告): {eligible}",
        f"- stocks with a representative: {len(picks)}",
        "",
        "## Notices by predicted type",
        "",
        "| type | count | share |",
        "|---|---|---|",
    ]
    for t, c in types.most_common():
        lines.append(f"| {t} | {c} | {_pct(c, n_notices)} |")

    lines += [
        "",
        f"## 其他 share (excluding 股價異常公告): {len(other)}/{len(classified)} "
        f"= {_pct(len(other), len(classified))} "
        "(collapsed notices; 其他 is a label gap, not necessarily a mistake)",
        "",
        "| group | 其他 | all | share |",
        "|---|---|---|---|",
    ]
    for key, label in (("market", "market"), ("day", "first published day")):
        groups: dict[str, list[int]] = {}
        for r, _ in classified:
            k = r["market"] if key == "market" else r["first_published_taipei"][:10]
            g = groups.setdefault(k, [0, 0])
            g[1] += 1
            g[0] += r["predicted_type"] == "其他"
        for k in sorted(groups):
            o, t = groups[k]
            lines.append(f"| {label} {k} | {o} | {t} | {_pct(o, t)} |")

    tiers = Counter(c.tier.name for c in picks.values())
    pick_types = Counter(c.event_type.value for c in picks.values())
    lines += [
        "",
        "## Representatives",
        "",
        "- by tier: " + ", ".join(f"{k} {v}" for k, v in tiers.most_common()),
        "- by type: " + ", ".join(f"{k} {v}" for k, v in pick_types.most_common()),
        f"- picked notice is 其他: {pick_types.get('其他', 0)} "
        "(review these first; this is NOT a count of missed events, because a "
        "missed important event can also lose to a higher-tier notice)",
        f"- picked notice is a repeat (occurrences > 1): "
        f"{sum(c.notice.occurrences > 1 for c in picks.values())}",
        f"- picked notice is a subsidiary notice: "
        f"{sum(c.is_subsidiary for c in picks.values())}",
        "",
        "## Most common 其他 title shapes (numbers replaced by N)",
        "",
        "| count | shape |",
        "|---|---|",
    ]
    shapes = Counter(_shape(r["title"]) for r, _ in other)
    for shape, c in shapes.most_common(25):
        lines.append(f"| {c} | {shape} |")
    lines += [
        "",
        "## Next",
        "",
        "Fill expected_type / review_note in samples.csv, then count: "
        "precision of 資產交易, important events hiding in 其他, picks that "
        "are not a sensible representative. Do not widen fuzzy keywords just "
        "to lower the 其他 share.",
    ]
    return "\n".join(lines) + "\n"


_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _safe_cell(value):
    """Stop Excel from running announcement text as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    # utf-8-sig so Excel opens the Chinese text correctly
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _safe_cell(v) for k, v in row.items()})


def load_official(session: Session, start: dt.date, end: dt.date) -> list[RawArticle]:
    """Read-only. Date bounds are Taipei calendar days, inclusive."""
    lo = dt.datetime.combine(start, dt.time.min, tzinfo=TAIPEI_TZ)
    hi = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=TAIPEI_TZ)
    stock_ids = set(
        session.scalars(
            select(RawArticleRow.stock_id)
            .where(
                RawArticleRow.is_official.is_(True),
                RawArticleRow.published_at >= lo,
                RawArticleRow.published_at < hi,
            )
            .distinct()
        )
    )
    if not stock_ids:
        return []
    return NewsRepository(session).load_window(stock_ids, start=start, end=end)


def run(
    session: Session,
    *,
    start: dt.date,
    end: dt.date,
    out_dir: Path,
    sample: int,
    stocks: frozenset[str],
) -> Analysis:
    if end < start:
        raise ValueError("--end must not be before --start")
    articles = load_official(session, start, end)
    result = analyze(articles, sample=sample, stocks=stocks, start=start, end=end)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "notices.csv", NOTICE_COLUMNS, result.notices_rows)
    _write_csv(out_dir / "samples.csv", SAMPLE_COLUMNS, result.sample_rows)
    (out_dir / "report.md").write_text(result.report, encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, default=Path("news_validation"))
    parser.add_argument("--sample", type=int, default=40)
    parser.add_argument("--stocks", default="", help="comma separated stock ids")
    args = parser.parse_args(argv)
    if args.sample < 1:
        parser.error("--sample must be at least 1")

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.error("DATABASE_URL is not set")
        return 1
    stocks = frozenset(s.strip() for s in args.stocks.split(",") if s.strip())
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            result = run(
                session,
                start=args.start,
                end=args.end,
                out_dir=args.out,
                sample=args.sample,
                stocks=stocks,
            )
    finally:
        engine.dispose()
    logger.info(
        "news_validate_ok notices=%d samples=%d out=%s",
        len(result.notices_rows),
        len(result.sample_rows),
        args.out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
