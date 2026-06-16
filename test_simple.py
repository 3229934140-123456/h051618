import sys, os
sys.path.insert(0, os.path.dirname(__file__))

print("Step 1: Import...")
from load_tester import HdrHistogram
print("OK")

print("Step 2: HDR Histogram...")
import random
hist = HdrHistogram(1_000, 60_000_000_000, 3)
random.seed(42)
for _ in range(100000):
    r = random.random()
    if r < 0.95:
        lat_ns = random.randint(50_000_000, 200_000_000)
    elif r < 0.99:
        lat_ns = random.randint(200_000_000, 1_000_000_000)
    else:
        lat_ns = random.randint(1_000_000_000, 5_000_000_000)
    hist.record_value(lat_ns)
print(f"  Count={hist.total_count}")
print(f"  P50={hist.get_value_at_percentile(50)/1e6:.2f}ms")
print(f"  P99={hist.get_value_at_percentile(99)/1e6:.2f}ms")
print("OK")

print("Step 3: Aggregator...")
from load_tester import MetricsAggregator, Sample, SampleStatus
import time
agg = MetricsAggregator()
samples = []
for i in range(1000):
    lat = 0.05 + (i % 100) * 0.001
    status = SampleStatus.SUCCESS if i < 950 else SampleStatus.ERROR
    samples.append(Sample(
        name=f'test_req_{i % 5}',
        latency=lat,
        status=status,
        timestamp=time.time(),
        status_code=200 if status == SampleStatus.SUCCESS else 500,
    ))
agg.write(samples)
m = agg.build()
print(f"  Total={m.throughput.total_requests}, Errors={m.errors.total_errors}")
print(f"  P50={m.overall.p50_ms:.2f}ms, Groups={len(m.by_name)}")
print("OK")

print("Step 4: Rate Limiter...")
from load_tester import TokenBucketRateLimiter
rl = TokenBucketRateLimiter(2000, busy_wait=False)
t0 = time.perf_counter()
for _ in range(500):
    rl.acquire(1)
elapsed = time.perf_counter() - t0
print(f"  500 tokens in {elapsed:.3f}s, rate={500/elapsed:.0f}/s")
print("OK")

print("Step 5: Scenario...")
from load_tester import (
    Scenario, ScenarioStep, HttpRequest, HttpMethod,
    SuccessAssertion, LatencyThresholdAssertion, RandomIntParameter,
)
scenario = Scenario(name='test', base_url='http://x.com')
scenario.add_parameter(RandomIntParameter('id', 1, 100))
scenario.add_step(ScenarioStep(
    name='get_user',
    request=HttpRequest(method=HttpMethod.GET, url='/users/${id}', name='get'),
    assertions=[SuccessAssertion(), LatencyThresholdAssertion(500)],
))
ctx = scenario.create_context('u1')
url = scenario.steps[0].request.resolve_url(ctx.variables)
print(f"  Steps={len(scenario.steps)}, Params={len(scenario.parameters)}")
print(f"  URL={url}")
print("OK")

print("\n" + "="*50)
print("ALL TESTS PASSED!")
print("="*50)
