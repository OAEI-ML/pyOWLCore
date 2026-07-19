"""Deterministic paired-bootstrap statistics for WP14 comparator gates."""

from __future__ import annotations

import hashlib
import math
import statistics
from collections.abc import Mapping, Sequence

MAX_U64 = 2**64 - 1
DEFAULT_BOOTSTRAP_RESAMPLES = 10_000
DEFAULT_CONFIDENCE_LEVEL = 0.95
BOOTSTRAP_INDEX_STREAM_SCHEMA = "pyowl-core/paired-bootstrap-index-stream/v1"
_BOOTSTRAP_INDEX_DOMAIN = b"pyowl-core:paired-bootstrap-index-stream:v1\x00"
_SHA256_RANGE = 1 << 256
_MAX_COUNTER = (1 << 128) - 1


class RatioStatisticsError(ValueError):
    """Raised when ratio evidence cannot support a valid paired estimate."""


class _Sha256BoundedIndexStream:
    """Cross-version deterministic bounded indexes without modulo bias."""

    __slots__ = ("_counter", "_seed")

    def __init__(self, seed: int) -> None:
        self._seed = _require_u64(seed, "seed")
        self._counter = 0

    def randbelow(self, upper_bound: int) -> int:
        upper = _require_u64(upper_bound, "upper_bound")
        if upper == 0:
            raise RatioStatisticsError("upper_bound must be positive")
        rejection_limit = _SHA256_RANGE - (_SHA256_RANGE % upper)
        while True:
            if self._counter > _MAX_COUNTER:  # unreachable under configured run limits
                raise RatioStatisticsError("bootstrap index counter is exhausted")
            preimage = (
                _BOOTSTRAP_INDEX_DOMAIN
                + self._seed.to_bytes(8, "big")
                + upper.to_bytes(8, "big")
                + self._counter.to_bytes(16, "big")
            )
            self._counter += 1
            candidate = int.from_bytes(hashlib.sha256(preimage).digest(), "big")
            if candidate < rejection_limit:
                return candidate % upper


def paired_bootstrap_ratio_summary(
    pairs_by_corpus: Mapping[str, Sequence[tuple[int, int]]],
    *,
    seed: int,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
) -> dict[str, object]:
    """Summarize paired numerator/denominator samples by corpus and aggregate.

    Each corpus contributes the median of its paired sample ratios. The aggregate
    is the geometric mean of those corpus medians, so corpus size and repetition
    count cannot silently reweight the required representative set. Bootstrap
    resampling preserves each numerator/denominator pair and is stratified by
    corpus.
    """

    _require_u64(seed, "seed")
    if isinstance(resamples, bool) or not isinstance(resamples, int):
        raise RatioStatisticsError("resamples must be an integer")
    if resamples < 1:
        raise RatioStatisticsError("resamples must be positive")
    if resamples > MAX_U64:
        raise RatioStatisticsError("resamples must fit unsigned 64-bit range")
    if isinstance(confidence_level, bool) or not isinstance(confidence_level, float):
        raise RatioStatisticsError("confidence_level must be a float")
    if not 0.0 < confidence_level < 1.0:
        raise RatioStatisticsError("confidence_level must be between zero and one")
    if not pairs_by_corpus:
        raise RatioStatisticsError("at least one corpus of paired samples is required")

    ratios_by_corpus: dict[str, tuple[float, ...]] = {}
    for corpus_id, pairs in sorted(pairs_by_corpus.items()):
        if not isinstance(corpus_id, str) or not corpus_id:
            raise RatioStatisticsError("corpus identifiers must be nonempty strings")
        if not pairs:
            raise RatioStatisticsError(f"{corpus_id}: paired samples are empty")
        ratios: list[float] = []
        for index, pair in enumerate(pairs):
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise RatioStatisticsError(
                    f"{corpus_id}: pair {index} must be a numerator/denominator tuple"
                )
            numerator = _require_positive_u64(pair[0], f"{corpus_id}: pair {index} numerator")
            denominator = _require_positive_u64(
                pair[1], f"{corpus_id}: pair {index} denominator"
            )
            ratio = numerator / denominator
            if not math.isfinite(ratio) or ratio <= 0.0:  # defensive if numeric types evolve
                raise RatioStatisticsError(f"{corpus_id}: pair {index} ratio is invalid")
            ratios.append(ratio)
        ratios_by_corpus[corpus_id] = tuple(ratios)

    point_medians = {
        corpus_id: float(statistics.median(ratios))
        for corpus_id, ratios in ratios_by_corpus.items()
    }
    point_aggregate = _geometric_mean(tuple(point_medians.values()))
    bootstrap_by_corpus: dict[str, list[float]] = {
        corpus_id: [] for corpus_id in ratios_by_corpus
    }
    bootstrap_aggregate: list[float] = []
    generator = _Sha256BoundedIndexStream(seed)
    for _ in range(resamples):
        replicate_medians: list[float] = []
        for corpus_id, corpus_ratios in ratios_by_corpus.items():
            count = len(corpus_ratios)
            replicate = tuple(
                corpus_ratios[generator.randbelow(count)] for _ in range(count)
            )
            replicate_median = float(statistics.median(replicate))
            bootstrap_by_corpus[corpus_id].append(replicate_median)
            replicate_medians.append(replicate_median)
        bootstrap_aggregate.append(_geometric_mean(tuple(replicate_medians)))

    tail = (1.0 - confidence_level) / 2.0
    corpus_rows: list[dict[str, object]] = []
    for corpus_id, corpus_ratios in ratios_by_corpus.items():
        bootstrap = bootstrap_by_corpus[corpus_id]
        corpus_rows.append(
            {
                "corpus_id": corpus_id,
                "paired_samples": len(corpus_ratios),
                "median_ratio": point_medians[corpus_id],
                "lower_confidence_bound": _quantile(bootstrap, tail),
                "upper_confidence_bound": _quantile(bootstrap, 1.0 - tail),
            }
        )
    return {
        "method": "stratified-paired-bootstrap-percentile",
        "confidence_level": confidence_level,
        "resamples": resamples,
        "seed": seed,
        "index_stream": {
            "schema": BOOTSTRAP_INDEX_STREAM_SCHEMA,
            "algorithm": "SHA-256 counter-derived candidates with rejection sampling",
            "preimage_format": (
                "UTF-8 domain plus NUL, u64 big-endian seed, u64 big-endian upper "
                "bound, and u128 big-endian counter"
            ),
            "candidate_interpretation": "unsigned 256-bit big-endian integer",
        },
        "corpus_statistic": "median of paired numerator/denominator ratios",
        "aggregate_statistic": "geometric mean of required-corpus median ratios",
        "corpora": corpus_rows,
        "aggregate": {
            "estimate": point_aggregate,
            "lower_confidence_bound": _quantile(bootstrap_aggregate, tail),
            "upper_confidence_bound": _quantile(bootstrap_aggregate, 1.0 - tail),
        },
    }


def _geometric_mean(values: Sequence[float]) -> float:
    if not values or any(not math.isfinite(value) or value <= 0.0 for value in values):
        raise RatioStatisticsError("geometric mean requires positive finite values")
    return math.exp(math.fsum(math.log(value) for value in values) / len(values))


def _quantile(values: Sequence[float], probability: float) -> float:
    """Return the linearly interpolated type-7 sample quantile."""

    if not values:
        raise RatioStatisticsError("a confidence interval requires bootstrap values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower_index = math.floor(position)
    upper_index = math.ceil(position)
    if lower_index == upper_index:
        return ordered[lower_index]
    fraction = position - lower_index
    return ordered[lower_index] + fraction * (ordered[upper_index] - ordered[lower_index])


def _require_u64(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RatioStatisticsError(f"{name} must be an integer")
    if value < 0 or value > MAX_U64:
        raise RatioStatisticsError(f"{name} must fit unsigned 64-bit range")
    return value


def _require_positive_u64(value: object, name: str) -> int:
    result = _require_u64(value, name)
    if result == 0:
        raise RatioStatisticsError(f"{name} must be positive")
    return result


__all__ = [
    "BOOTSTRAP_INDEX_STREAM_SCHEMA",
    "DEFAULT_BOOTSTRAP_RESAMPLES",
    "DEFAULT_CONFIDENCE_LEVEL",
    "MAX_U64",
    "RatioStatisticsError",
    "paired_bootstrap_ratio_summary",
]
