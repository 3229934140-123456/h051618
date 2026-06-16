"""基础模块验证脚本"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


def step1_import():
    print("=== Step 1: Importing all modules ===")
    from load_tester import (
        LoadTestConfig, LoadTestEngine, LoadTestResult,
        Scenario, ScenarioStep, HttpRequest, HttpMethod,
        SuccessAssertion, JsonPathAssertion, LatencyThresholdAssertion,
        WorkerPool, Worker, TokenBucketRateLimiter, ConstantLoadModel,
        SpikeLoadModel, RampUpLoadModel, StepLoadModel,
        LeakyBucketRateLimiter,
        MetricsCollector, MetricsSink, RealTimeSink,
        RecordingSink, WindowedSink,
        Sample, SampleStatus, SampleType,
        ConsoleReporter, JsonReporter, HtmlReporter,
        HdrHistogram, MetricsAggregator,
        AggregatedMetrics, PercentileMetrics, ThroughputMetrics, ErrorMetrics,
    )
    from load_tester.scenario import (
        StatusCodeAssertion, StatusCodeInAssertion,
        BodyContainsAssertion, BodyMatchesAssertion,
        HeaderExistsAssertion, HeaderValueAssertion,
        CustomAssertion, CustomParameter,
        Extractor,
        ConstantParameter, RandomIntParameter, RandomFloatParameter,
        RandomStringParameter, RandomChoiceParameter,
        SequenceParameter, UuidParameter, CounterParameter,
        CsvParameter, DatetimeParameter, TimestampParameter,
        RequestResult, ResponseData, ScenarioContext, ScenarioResult,
        ScenarioStepResult,
    )
    from load_tester.stats import HistogramSnapshot
    print("OK: All imports successful")


def step2_hdr_histogram():
    print()
    print("=== Step 2: HDR Histogram Test ===")
    import random
    from load_tester import HdrHistogram

    hist = HdrHistogram(1_000, 60_000_000_000, 3)
    random.seed(42)
    for _ in range(100000):
        r = random.random()
        if r < 0.95:
            lat_ns = random.randint(50_000_000, 200_000_000)  # 50-200ms
        elif r < 0.99:
            lat_ns = random.randint(200_000_000, 1_000_000_000)  # 200ms-1s
        else:
            lat_ns = random.randint(1_000_000_000, 5_000_000_000)  # 1s-5s
        hist.record_value(lat_ns)

    print(f"Count: {hist.total_count}")
    p50 = hist.get_value_at_percentile(50) / 1e6
    p90 = hist.get_value_at_percentile(90) / 1e6
    p99 = hist.get_value_at_percentile(99) / 1e6
    p999 = hist.get_value_at_percentile(99.9) / 1e6
    print(f"P50: {p50:.2f}ms (expect ~125ms)")
    print(f"P90: {p90:.2f}ms (expect ~195ms)")
    print(f"P99: {p99:.2f}ms (expect ~600-800ms)")
    print(f"P99.9: {p999:.2f}ms (expect ~3-4.5s)")
    print(f"Max: {hist.max_value_ns/1e6:.2f}ms")
    print(f"Mean: {hist.get_mean()/1e6:.2f}ms")
    print(f"StdDev: {hist.get_stddev()/1e6:.2f}ms")
    # 断言基本合理性
    assert 100 < p50 < 150, f"P50 out of range: {p50}"
    assert 180 < p90 < 210, f"P90 out of range: {p90}"
    assert 400 < p99 < 1000, f"P99 out of range: {p99}"
    print("OK: HDR Histogram works correctly")


def step3_aggregator():
    print()
    print("=== Step 3: Metrics Aggregator Test ===")
    import time
    from load_tester import MetricsAggregator, Sample, SampleStatus

    agg = MetricsAggregator()
    samples = []
    for i in range(1000):
        lat = 0.05 + (i % 100) * 0.001  # 50ms ~ 150ms
        status = SampleStatus.SUCCESS if i < 950 else SampleStatus.ERROR
        s = Sample(
            name=f'test_req_{i % 5}',
            latency=lat,
            status=status,
            timestamp=time.time(),
            status_code=200 if status == SampleStatus.SUCCESS else 500,
        )
        samples.append(s)
    agg.write(samples)
    metrics = agg.build()
    print(f"Total: {metrics.throughput.total_requests}")
    print(f"Success: {metrics.throughput.total_success}")
    print(f"Failures: {metrics.throughput.total_failures}")
    print(f"Overall QPS: {metrics.throughput.overall_qps:.2f}")
    print(f"P50: {metrics.overall.p50_ms:.2f}ms")
    print(f"P99: {metrics.overall.p99_ms:.2f}ms")
    print(f"Error count: {metrics.errors.total_errors}")
    print(f"Error rate: {metrics.errors.error_rate*100:.2f}%")
    print(f"By name groups: {len(metrics.by_name)}")
    assert metrics.throughput.total_requests == 1000
    assert metrics.errors.total_errors == 50
    assert len(metrics.by_name) == 5
    print("OK: Metrics Aggregator works")


def step4_rate_limiter():
    print()
    print("=== Step 4: Rate Limiter Test ===")
    import time
    from load_tester import TokenBucketRateLimiter

    rl = TokenBucketRateLimiter(2000, busy_wait=False)
    start = time.perf_counter()
    for _ in range(1000):
        rl.acquire(1)
    elapsed = time.perf_counter() - start
    actual_rate = 1000 / elapsed
    print(f"1000 tokens at 2000/s: elapsed={elapsed:.3f}s (expected ~0.5s)")
    print(f"Actual rate: {actual_rate:.1f} tokens/s (expect ~2000)")
    # 因为起始桶是满的，所以可能比理论值快，但不能超过太多
    assert 0.4 < elapsed < 1.0, f"Elapsed time out of range: {elapsed}"
    print("OK: Rate Limiter works")

    # 动态调整测试
    rl.set_rate(500)
    time.sleep(0.01)
    start = time.perf_counter()
    for _ in range(100):
        rl.acquire(1)
    elapsed = time.perf_counter() - start
    print(f"After set_rate(500): 100 tokens in {elapsed:.3f}s (expect ~0.2s)")


def step5_scenario():
    print()
    print("=== Step 5: Scenario definition test ===")
    from load_tester import (
        Scenario, ScenarioStep, HttpRequest, HttpMethod,
        SuccessAssertion, LatencyThresholdAssertion,
        RandomIntParameter, JsonPathAssertion, Extractor,
    )
    from load_tester.scenario.assertion import StatusCodeInAssertion

    scenario = Scenario(name='test_scenario', base_url='http://test.example.com')
    scenario.add_parameter(RandomIntParameter('id', 1, 100))
    scenario.add_step(ScenarioStep(
        name='get_user',
        request=HttpRequest(
            method=HttpMethod.GET,
            url='/api/users/${id}',
            name='get_user_api',
        ),
        assertions=[
            SuccessAssertion(),
            LatencyThresholdAssertion(500),
        ],
        extractors=[
            Extractor(name='user_id', target='data.id', source='json_path'),
        ],
        think_time=0.3,
    ))
    scenario.add_step(ScenarioStep(
        name='update_user',
        request=HttpRequest(
            method=HttpMethod.PUT,
            url='/api/users/${user_id}',
            body={'id': '${user_id}', 'name': 'test_${id}'},
        ),
        assertions=[
            StatusCodeInAssertion([200, 204]),
            JsonPathAssertion(json_path='success', expected_value=True),
        ],
    ))

    ctx = scenario.create_context('user_test_001')
    print(f'Context id (random in 1-100): {ctx.variables.get("id")}')
    print(f'Worker id: {ctx.user_id}')
    print(f'Steps: {len(scenario.steps)}')
    print(f'Parameters: {len(scenario.parameters)}')

    # 测试 URL 解析
    req0 = scenario.steps[0].request
    resolved_url = req0.resolve_url(ctx.variables)
    print(f'Resolved URL for step1: {resolved_url}')
    assert 'test.example.com' in resolved_url
    assert '/api/users/' in resolved_url
    print(f'Step1 assertions: {len(scenario.steps[0].assertions)}')
    print(f'Step1 extractors: {len(scenario.steps[0].extractors)}')
    print('OK: Scenario definition works')


def step6_reporting():
    print()
    print("=== Step 6: Reporter Test ===")
    from load_tester import MetricsAggregator, Sample, SampleStatus, ConsoleReporter, JsonReporter
    import time, json, tempfile, os

    # Build some metrics
    agg = MetricsAggregator()
    samples = []
    for i in range(2000):
        import random
        lat = 0.03 + random.random() * 0.1  # 30-130ms
        status = SampleStatus.SUCCESS if i < 1950 else SampleStatus.ERROR
        samples.append(Sample(
            name=f'op_{i % 3}',
            latency=lat,
            status=status,
            timestamp=time.time() - (2000 - i) * 0.01,
            status_code=200 if status == SampleStatus.SUCCESS else 500,
        ))
    agg.write(samples)
    metrics = agg.build()

    # Console reporter (no color to avoid ANSI in test)
    print()
    print("  Console Reporter output (truncated):")
    reporter = ConsoleReporter(color=False, show_progress=False)
    out = reporter.report(metrics, title="Test Report")
    lines = [l for l in out.split('\n') if l.strip()][:10]
    for l in lines:
        print(f"    {l}")
    print("    ...")

    # JSON reporter
    with tempfile.NamedTemporaryFile(suffix='.json', delete=False, mode='w') as f:
        tmp_path = f.name
    try:
        JsonReporter().report(metrics, tmp_path, title="Test JSON Report")
        with open(tmp_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        print(f"  JSON report: keys={list(data.keys())}")
        print(f"  Summary total_requests: {data['summary']['total_requests']}")
        print(f"  Latency p99_ms: {data['latency']['p99_ms']:.2f}")
        assert data['summary']['total_requests'] == 2000
    finally:
        os.unlink(tmp_path)
    print("OK: Reporters work")


def main():
    try:
        step1_import()
        step2_hdr_histogram()
        step3_aggregator()
        step4_rate_limiter()
        step5_scenario()
        step6_reporting()
        print()
        print("=" * 60)
        print("✅ ALL MODULE TESTS PASSED!")
        print("=" * 60)
        return 0
    except Exception as e:
        import traceback
        print(f"\n❌ FAILED: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
