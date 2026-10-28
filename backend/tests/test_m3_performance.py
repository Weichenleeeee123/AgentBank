from agentbank.evals.performance import benchmark_fraud_detector


def test_fraud_benchmark_reports_measured_latency() -> None:
    result = benchmark_fraud_detector(iterations=20)
    assert result.iterations == 20
    assert result.p50_ms >= 0
    assert result.p95_ms >= result.p50_ms
