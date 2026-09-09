import pandas as pd

from app.domain.normalization import rank_within_pool, relative_score_sample_size


# --- relative_score_sample_size ----------------------------------------------


def test_sample_size_counts_only_non_missing_values():
    series = pd.Series([1.0, 2.0, None, 4.0])
    assert relative_score_sample_size(series) == 3


def test_sample_size_zero_when_all_missing():
    series = pd.Series([None, None, None])
    assert relative_score_sample_size(series) == 0


def test_sample_size_zero_for_empty_series():
    series = pd.Series([], dtype=float)
    assert relative_score_sample_size(series) == 0


# --- rank_within_pool ---------------------------------------------------------


def test_rank_higher_is_better_gives_rank_1_to_largest_value():
    series = pd.Series([10.0, 30.0, 20.0], index=["A", "B", "C"])
    ranks = rank_within_pool(series, higher_is_better=True)
    assert ranks["B"] == 1
    assert ranks["C"] == 2
    assert ranks["A"] == 3


def test_rank_lower_is_better_gives_rank_1_to_smallest_value():
    series = pd.Series([10.0, 30.0, 20.0], index=["A", "B", "C"])
    ranks = rank_within_pool(series, higher_is_better=False)
    assert ranks["A"] == 1
    assert ranks["C"] == 2
    assert ranks["B"] == 3


def test_rank_missing_values_stay_missing():
    series = pd.Series([10.0, None, 20.0], index=["A", "B", "C"])
    ranks = rank_within_pool(series, higher_is_better=True)
    assert ranks["C"] == 1
    assert ranks["A"] == 2
    assert pd.isna(ranks["B"])


def test_rank_ties_share_the_same_minimum_rank():
    """Two equal values should tie at the better rank (method='min'),
    e.g. two tied-for-best values both get rank 1, and the next value
    gets rank 3, not 2 — this matches "N candidates are equally best"
    more honestly than an arbitrary rank(2) that implies a distinction
    with no difference."""
    series = pd.Series([30.0, 30.0, 10.0], index=["A", "B", "C"])
    ranks = rank_within_pool(series, higher_is_better=True)
    assert ranks["A"] == 1
    assert ranks["B"] == 1
    assert ranks["C"] == 3
