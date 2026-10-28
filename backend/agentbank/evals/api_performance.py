from __future__ import annotations

from dataclasses import dataclass
from statistics import quantiles
from time import perf_counter
from typing import Callable


@dataclass(frozen=True)
class ApiPerformanceResult:
    iterations: int
    p50_ms: float
    p95_ms: float


def benchmark_callable(operation: Callable[[], object], iterations: int = 20) -> ApiPerformanceResult:
    samples: list[float] = []
    for _ in range(iterations):
        started = perf_counter()
        operation()
        samples.append((perf_counter() - started) * 1000)
    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2]
    p95 = quantiles(ordered, n=20, method="inclusive")[18] if len(ordered) >= 2 else p50
    return ApiPerformanceResult(iterations, round(p50, 3), round(p95, 3))
