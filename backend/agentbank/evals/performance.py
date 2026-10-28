from __future__ import annotations

from dataclasses import dataclass
from statistics import quantiles
from time import perf_counter

from agentbank.security.fraud import FraudDetector


@dataclass(frozen=True)
class PerformanceResult:
    iterations: int
    p50_ms: float
    p95_ms: float


def benchmark_fraud_detector(iterations: int = 100) -> PerformanceResult:
    detector = FraudDetector()
    samples: list[float] = []
    for _ in range(iterations):
        started = perf_counter()
        detector.evaluate("客服说转到安全账户并马上转5000", relation={"new_recipient": True}, behavior={"night": True})
        samples.append((perf_counter() - started) * 1000)
    ordered = sorted(samples)
    p50 = ordered[len(ordered) // 2]
    p95 = quantiles(ordered, n=20, method="inclusive")[18] if len(ordered) >= 2 else p50
    return PerformanceResult(iterations, round(p50, 3), round(p95, 3))
