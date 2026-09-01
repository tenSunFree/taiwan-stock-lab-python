import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.eps_observation_repository import EpsObservationRepository
from app.db.models import EpsCumulativeObservation, IngestionRun

RUN_1 = "20260811-090000-aaaa"
RUN_2 = "20260812-090000-bbbb"


@pytest.fixture()
def session():
    # In-memory SQLite is enough to exercise the UNIQUE-constraint
    # behavior this repository relies on — same approach as
    # test_delivery_repository.py.
    engine = create_engine("sqlite:///:memory:")
    IngestionRun.__table__.create(engine)
    EpsCumulativeObservation.__table__.create(engine)
    with Session(engine) as s:
        # Both ingestion runs referenced below must exist first —
        # first_seen_ingestion_run_id is a real FK, unlike
        # MessageDelivery (which has none).
        s.add(
            IngestionRun(
                ingestion_run_id=RUN_1,
                target_date=dt.date(2026, 8, 11),
                started_at=dt.datetime(2026, 8, 11, 9, 0, tzinfo=dt.timezone.utc),
                status="SUCCESS",
            )
        )
        s.add(
            IngestionRun(
                ingestion_run_id=RUN_2,
                target_date=dt.date(2026, 8, 12),
                started_at=dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.timezone.utc),
                status="SUCCESS",
            )
        )
        s.commit()
        yield s


def _observe_1101_q2(session, *, cumulative_eps, first_seen_at, ingestion_run_id):
    repo = EpsObservationRepository(session)
    return repo.observe(
        stock_id="1101",
        market="twse",
        fiscal_year=2026,
        quarter=2,
        cumulative_eps=cumulative_eps,
        batch_report_date=dt.date(2026, 8, 31),
        first_seen_at=first_seen_at,
        ingestion_run_id=ingestion_run_id,
    )


def test_observe_creates_a_new_row_for_a_never_seen_value(session):
    result = _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    assert result.created is True
    assert result.observation.first_seen_at == dt.date(2026, 8, 11)
    assert result.observation.first_seen_ingestion_run_id == RUN_1


def test_observe_same_value_again_returns_existing_row_first_seen_at_unchanged(
    session,
):
    """
    THE core invariant: a later run re-observing the textually
    identical figure must NOT overwrite first_seen_at with its own,
    later date.
    """
    first = _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    second = _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 12),  # a day later, must be discarded
        ingestion_run_id=RUN_2,
    )

    assert second.created is False
    assert second.observation.id == first.observation.id
    assert second.observation.first_seen_at == dt.date(2026, 8, 11)
    assert second.observation.first_seen_ingestion_run_id == RUN_1


def test_observe_a_revised_value_creates_a_separate_row_with_its_own_first_seen_at(
    session,
):
    """
    A restatement (different cumulative_eps for the same period) must
    NOT be treated as "already observed" — it needs its own row and
    its own first_seen_at, while the original row is left completely
    untouched.
    """
    original = _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    revised = _observe_1101_q2(
        session,
        cumulative_eps=0.40,  # restated
        first_seen_at=dt.date(2026, 8, 12),
        ingestion_run_id=RUN_2,
    )

    assert revised.created is True
    assert revised.observation.id != original.observation.id
    assert revised.observation.first_seen_at == dt.date(2026, 8, 12)

    # The original row must still exist, unmodified.
    assert original.observation.first_seen_at == dt.date(2026, 8, 11)
    assert float(original.observation.cumulative_eps) == pytest.approx(0.38)


def test_float_precision_does_not_defeat_the_unique_constraint(session):
    """
    0.1 + 0.2 != 0.3 in raw IEEE-754 float arithmetic — if this
    repository compared floats directly instead of going through
    _to_decimal()'s str()-based conversion, two runs both meaning
    "0.38" could end up compared as different values purely due to
    binary floating-point noise, silently defeating the whole
    revision-safety guarantee this table exists for.
    """
    first = _observe_1101_q2(
        session,
        cumulative_eps=0.1 + 0.2 - 0.3 + 0.38,  # == 0.38 in decimal terms
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    second = _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 12),
        ingestion_run_id=RUN_2,
    )
    assert second.created is False
    assert second.observation.id == first.observation.id


def test_get_first_seen_at_returns_the_recorded_date(session):
    _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    repo = EpsObservationRepository(session)
    result = repo.get_first_seen_at(
        stock_id="1101", fiscal_year=2026, quarter=2, cumulative_eps=0.38
    )
    assert result == dt.date(2026, 8, 11)


def test_get_first_seen_at_returns_none_for_a_value_never_observed(session):
    repo = EpsObservationRepository(session)
    result = repo.get_first_seen_at(
        stock_id="1101", fiscal_year=2026, quarter=2, cumulative_eps=0.38
    )
    assert result is None


def test_get_first_seen_at_distinguishes_revisions_by_value(session):
    """
    A revised value must resolve to ITS OWN first_seen_at, not the
    original period's — this is exactly what
    eps_availability_resolver.build_resolved_cumulative_eps_point
    depends on to avoid look-ahead bias on a restated figure.
    """
    _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    _observe_1101_q2(
        session,
        cumulative_eps=0.40,
        first_seen_at=dt.date(2026, 8, 12),
        ingestion_run_id=RUN_2,
    )
    repo = EpsObservationRepository(session)

    assert repo.get_first_seen_at(
        stock_id="1101", fiscal_year=2026, quarter=2, cumulative_eps=0.38
    ) == dt.date(2026, 8, 11)
    assert repo.get_first_seen_at(
        stock_id="1101", fiscal_year=2026, quarter=2, cumulative_eps=0.40
    ) == dt.date(2026, 8, 12)


def test_different_stock_ids_are_independent(session):
    _observe_1101_q2(
        session,
        cumulative_eps=0.38,
        first_seen_at=dt.date(2026, 8, 11),
        ingestion_run_id=RUN_1,
    )
    repo = EpsObservationRepository(session)
    other = repo.observe(
        stock_id="1102",
        market="twse",
        fiscal_year=2026,
        quarter=2,
        cumulative_eps=0.38,  # same value, different company
        batch_report_date=dt.date(2026, 8, 31),
        first_seen_at=dt.date(2026, 8, 12),
        ingestion_run_id=RUN_2,
    )
    assert other.created is True
    assert other.observation.first_seen_at == dt.date(2026, 8, 12)
