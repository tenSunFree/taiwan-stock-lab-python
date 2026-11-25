import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.news_models import ensure_news_tables
from app.db.news_repository import CollectionRunRepository
from app.news.models import TAIPEI_TZ
from app.news.runs import CollectionRun

T0 = dt.datetime(2026, 11, 18, 15, 17, tzinfo=TAIPEI_TZ)


def run_(market="TWSE", *, start=T0, secs=30, success=True, **kw):
    return CollectionRun(
        run_id=kw.pop("run_id", "r1"),
        market=market,
        started_at=start,
        finished_at=start + dt.timedelta(seconds=secs),
        success=success,
        **kw,
    )


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    ensure_news_tables(engine)
    ensure_news_tables(engine)  # idempotent
    with Session(engine) as s:
        yield s


def test_dataclass_rejects_naive_times_and_inverted_range():
    with pytest.raises(ValueError):
        CollectionRun("r", "TWSE", T0.replace(tzinfo=None), T0, True)
    with pytest.raises(ValueError):
        CollectionRun("r", "TWSE", T0, T0 - dt.timedelta(seconds=1), True)
    with pytest.raises(ValueError):
        CollectionRun("r", "TWSE", T0, T0, True, error="X")


def test_roundtrip_keeps_every_field(session):
    repo = CollectionRunRepository(session)
    rec = run_(
        raw_rows=10,
        mapped_rows=9,
        inserted_rows=4,
        first_published_date=dt.date(2026, 11, 17),
        last_published_date=dt.date(2026, 11, 17),
    )
    repo.record(rec)
    assert repo.successful_finished_by(T0 + dt.timedelta(hours=1)) == [rec]


def test_utc_input_is_stored_as_the_same_instant(session):
    repo = CollectionRunRepository(session)
    utc = run_(start=T0.astimezone(dt.timezone.utc))
    repo.record(utc)
    (got,) = repo.successful_finished_by(T0 + dt.timedelta(hours=1))
    assert got.started_at == T0 and got.finished_at == T0 + dt.timedelta(seconds=30)


def test_only_runs_finished_by_the_cutoff_count_and_the_boundary_is_inclusive(session):
    repo = CollectionRunRepository(session)
    repo.record(run_(run_id="a", start=T0, secs=60))  # finishes 15:18:00
    repo.record(run_(run_id="b", start=T0 + dt.timedelta(hours=1, minutes=42)))
    cutoff = T0 + dt.timedelta(seconds=60)
    assert [r.run_id for r in repo.successful_finished_by(cutoff)] == ["a"]
    # one second earlier the run has not finished yet
    assert repo.successful_finished_by(cutoff - dt.timedelta(seconds=1)) == []


def test_a_run_that_started_before_but_finished_after_the_cutoff_is_excluded(session):
    repo = CollectionRunRepository(session)
    cutoff = dt.datetime(2026, 11, 18, 17, 0, tzinfo=TAIPEI_TZ)
    repo.record(run_(start=cutoff - dt.timedelta(minutes=1), secs=150))  # 16:59->17:01
    assert repo.successful_finished_by(cutoff) == []


def test_failed_runs_never_count(session):
    repo = CollectionRunRepository(session)
    repo.record(run_(success=False, error="HTTPStatusError"))
    assert repo.successful_finished_by(T0 + dt.timedelta(days=1)) == []


def test_market_filter_and_ordering(session):
    repo = CollectionRunRepository(session)
    repo.record(run_("TPEX", run_id="x", start=T0 + dt.timedelta(hours=6)))
    repo.record(run_("TWSE", run_id="y", start=T0))
    got = repo.successful_finished_by(T0 + dt.timedelta(days=1))
    assert [r.market for r in got] == ["TWSE", "TPEX"]
    only = repo.successful_finished_by(T0 + dt.timedelta(days=1), market="TPEX")
    assert [r.run_id for r in only] == ["x"]


def test_naive_cutoff_is_rejected(session):
    with pytest.raises(ValueError):
        CollectionRunRepository(session).successful_finished_by(T0.replace(tzinfo=None))


def test_duplicate_run_id_and_market_is_rejected(session):
    from sqlalchemy.exc import IntegrityError

    repo = CollectionRunRepository(session)
    repo.record(run_(run_id="dup"))
    repo.record(run_("TPEX", run_id="dup"))  # same run, other market: fine
    with pytest.raises(IntegrityError):
        repo.record(run_(run_id="dup"))


def test_snapshot_horizon_is_the_start_of_the_latest_run_finished_by_cutoff(session):
    repo = CollectionRunRepository(session)
    cutoff = dt.datetime(2026, 11, 18, 17, 0, tzinfo=TAIPEI_TZ)
    early = dt.datetime(2026, 11, 18, 9, 17, tzinfo=TAIPEI_TZ)
    mid = dt.datetime(2026, 11, 18, 15, 17, tzinfo=TAIPEI_TZ)
    straddle = cutoff - dt.timedelta(minutes=1)
    repo.record(run_(run_id="a", start=early))
    repo.record(run_(run_id="b", start=mid))
    repo.record(run_(run_id="c", start=straddle, secs=150))  # ends 17:01
    assert repo.snapshot_horizon(cutoff, market="TWSE") == mid
    # the same cutoff gives the same answer however late it is asked
    repo.record(run_(run_id="d", start=cutoff + dt.timedelta(hours=4)))
    assert repo.snapshot_horizon(cutoff, market="TWSE") == mid


def test_snapshot_horizon_ignores_failed_runs_and_other_markets(session):
    repo = CollectionRunRepository(session)
    cutoff = T0 + dt.timedelta(hours=3)
    assert repo.snapshot_horizon(cutoff, market="TWSE") is None
    repo.record(run_(success=False, error="X", start=T0 + dt.timedelta(hours=1)))
    repo.record(run_("TPEX", start=T0 + dt.timedelta(hours=2)))
    assert repo.snapshot_horizon(cutoff, market="TWSE") is None
    repo.record(run_(run_id="ok", start=T0))
    assert repo.snapshot_horizon(cutoff, market="TWSE") == T0
