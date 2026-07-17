"""Stable raw-sample statistics used by benchmark and regression reports."""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Iterable
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Sample:
    """One validated operation sample."""

    wall_ns: int
    cpu_ns: int
    allocated_current_bytes: int
    allocated_peak_bytes: int
    rss_peak_bytes: int
    fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "wall_ns",
            "cpu_ns",
            "allocated_current_bytes",
            "allocated_peak_bytes",
            "rss_peak_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be int")
            if value < 0:
                raise ValueError(f"{name} must not be negative")
        if not isinstance(self.fingerprint, str) or not self.fingerprint:
            raise ValueError("fingerprint must be a nonempty string")

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Distribution:
    """Robust summary retaining a deterministic median confidence interval."""

    count: int
    minimum: float
    median: float
    p90: float
    p95: float
    maximum: float
    mad: float
    median_ci95_low: float
    median_ci95_high: float

    def as_dict(self) -> dict[str, int | float]:
        return asdict(self)


def summarize(values: Iterable[int | float], *, bootstrap_seed: int = 0x0A10) -> Distribution:
    """Summarize finite non-negative values; fail closed on missing/invalid samples."""

    retained = tuple(float(value) for value in values)
    if not retained:
        raise ValueError("at least one sample is required")
    if any(not math.isfinite(value) or value < 0 for value in retained):
        raise ValueError("samples must be finite and non-negative")
    ordered = tuple(sorted(retained))
    median = statistics.median(ordered)
    deviations = tuple(abs(value - median) for value in ordered)
    ci_low, ci_high = _bootstrap_median_ci(ordered, bootstrap_seed)
    return Distribution(
        count=len(ordered),
        minimum=ordered[0],
        median=median,
        p90=_percentile(ordered, 0.90),
        p95=_percentile(ordered, 0.95),
        maximum=ordered[-1],
        mad=statistics.median(deviations),
        median_ci95_low=ci_low,
        median_ci95_high=ci_high,
    )


def _percentile(ordered: tuple[float, ...], fraction: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _bootstrap_median_ci(values: tuple[float, ...], seed: int) -> tuple[float, float]:
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    iterations = max(1_000, 100 * len(values))
    medians = tuple(
        sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(iterations))
    )
    return _percentile(medians, 0.025), _percentile(medians, 0.975)


__all__ = ["Distribution", "Sample", "summarize"]
