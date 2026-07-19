from __future__ import annotations

import math
from typing import Any, cast

import pytest

from tools.benchmark.comparators.ratio_statistics import (
    BOOTSTRAP_INDEX_STREAM_SCHEMA,
    MAX_U64,
    RatioStatisticsError,
    _Sha256BoundedIndexStream,
    paired_bootstrap_ratio_summary,
)


def test_paired_bootstrap_is_seeded_and_aggregates_corpus_medians_geometrically() -> None:
    pairs = {
        "medium": ((90, 100), (100, 100), (110, 100)),
        "large": ((200, 100), (200, 100), (200, 100)),
    }

    first = paired_bootstrap_ratio_summary(pairs, seed=42, resamples=500)
    second = paired_bootstrap_ratio_summary(pairs, seed=42, resamples=500)

    assert first == second
    aggregate = cast(dict[str, Any], first["aggregate"])
    assert aggregate["estimate"] == pytest.approx(math.sqrt(2.0))
    corpora = {
        value["corpus_id"]: value
        for value in cast(list[dict[str, Any]], first["corpora"])
    }
    assert corpora["medium"]["median_ratio"] == 1.0
    assert corpora["large"]["median_ratio"] == 2.0
    assert first["aggregate_statistic"] == (
        "geometric mean of required-corpus median ratios"
    )
    assert cast(dict[str, Any], first["index_stream"])["schema"] == (
        BOOTSTRAP_INDEX_STREAM_SCHEMA
    )


def test_sha256_bounded_index_stream_has_a_frozen_vector() -> None:
    stream = _Sha256BoundedIndexStream(42)

    assert [stream.randbelow(5) for _ in range(12)] == [
        2,
        4,
        1,
        2,
        4,
        1,
        1,
        2,
        0,
        1,
        0,
        4,
    ]


@pytest.mark.parametrize(
    "pairs, reason",
    [
        ({}, "at least one corpus"),
        ({"medium": ()}, "paired samples are empty"),
        ({"medium": ((0, 1),)}, "numerator must be positive"),
        ({"medium": ((1, 0),)}, "denominator must be positive"),
        ({"medium": ((True, 1),)}, "numerator must be an integer"),
        ({"medium": ((MAX_U64 + 1, 1),)}, "unsigned 64-bit"),
    ],
)
def test_paired_bootstrap_rejects_missing_invalid_or_nonpositive_evidence(
    pairs: dict[str, tuple[tuple[int, int], ...]],
    reason: str,
) -> None:
    with pytest.raises(RatioStatisticsError, match=reason):
        paired_bootstrap_ratio_summary(pairs, seed=0, resamples=10)


@pytest.mark.parametrize("seed", [-1, MAX_U64 + 1, True, 1.0])
def test_paired_bootstrap_seed_is_exact_u64(seed: object) -> None:
    with pytest.raises(RatioStatisticsError):
        paired_bootstrap_ratio_summary({"medium": ((1, 1),)}, seed=cast(Any, seed))
