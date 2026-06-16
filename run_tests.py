"""简化版综合测试 - 避免编码问题"""
import os
import sys
import json
import tempfile
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from load_tester.scenario.scenario import (
    Scenario, ScenarioStep, ScenarioContext,
)
from load_tester.scenario.request import HttpRequest, HttpMethod, ResponseData
from load_tester.scenario.parameter import (
    ParameterSet, CounterParameter, CsvParameter, CsvReadMode,
)
from load_tester.engine import LoadTestEngine, LoadTestConfig
from load_tester.report.json_report import JsonReporter
from load_tester.report.html_report import HtmlReporter
import time

PASS = 0
FAIL = 0
RESULTS = []

def run_test(name, func):
    global PASS, FAIL
    try:
        func()
        PASS += 1
        RESULTS.append(("[PASS]", name))
        print("[PASS]", name)
    except Exception as e:
        FAIL += 1
        RESULTS.append(("[FAIL]", name, str(e)[:200]))
        print("[FAIL]", name, "-", str(e)[:200])

def mock_exec(req):
    import random
    time.sleep(0.002)
    status = 200
    error = None
    r = random.random()
    if r < 0.03:
        status = 500
        error = "Internal Server Error"
    elif r < 0.05:
        status = 404
        error = "Not Found"
    return ResponseData(
        status_code=status,
        body="{}",
        headers={},
        latency=0.002 + random.random() * 0.01,
        timestamp=time.time(),
        error=error,
    )

# ========= 测试1：计数器 =========
def test_counter():
    counter = CounterParameter("req_id", start=1, step=1)
    ps = ParameterSet([counter])
    vals = [int(ps.generate()["req_id"]) for _ in range(10)]
    assert vals == list(range(1, 11)), f"Counter wrong: {vals}"
    stats = ps.get_stats()[0]
    assert stats["call_count"] == 10

run_test("Counter continuous increment", test_counter)

# ========= 测试2：CSV 顺序模式 =========
def test_csv_seq():
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_p = CsvParameter(
        name="u", csv_path=csv_path, mode=CsvReadMode.SEQUENTIAL, loop=True,
    )
    ps = ParameterSet([csv_p])
    rows = [ps.generate()["u"]["category"] for _ in range(12)]
    assert rows[0] == "electronics" and rows[10] == "electronics", f"Seq loop wrong: {rows}"
    s = ps.get_csv_stats()[0]
    assert s["total_rows_total"] == 10
    assert s["loop_count"] == 1

run_test("CSV sequential + loop + stats", test_csv_seq)

# ========= 测试3：CSV Worker分片 =========
def test_csv_shard():
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    p0 = CsvParameter(name="u", csv_path=csv_path, mode=CsvReadMode.WORKER_SHARDED, loop=True)
    p0.set_worker_context("worker-0", 2)
    p1 = CsvParameter(name="u", csv_path=csv_path, mode=CsvReadMode.WORKER_SHARDED, loop=True)
    p1.set_worker_context("worker-1", 2)
    ps0 = ParameterSet([p0])
    ps1 = ParameterSet([p1])
    pids0 = [int(ps0.generate()["u"]["product_id"]) for _ in range(5)]
    pids1 = [int(ps1.generate()["u"]["product_id"]) for _ in range(5)]
    assert pids0 == [1001, 1002, 2001, 2002, 3001], f"W0 shard: {pids0}"
    assert pids1 == [3002, 4001, 4002, 5001, 5002], f"W1 shard: {pids1}"

run_test("CSV worker sharded", test_csv_shard)

# ========= 测试4：点号路径访问 =========
def test_dot_path():
    scenario = Scenario(name="test")
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_p = CsvParameter(name="user", csv_path=csv_path, mode=CsvReadMode.SEQUENTIAL, loop=True)
    counter_p = CounterParameter("order_id", start=1000)
    scenario.parameters = ParameterSet([csv_p, counter_p])
    scenario.add_step(ScenarioStep(
        name="view",
        request=HttpRequest(
            name="v",
            method=HttpMethod.GET,
            url="http://api.com/p/${user.product_id}",
            query_params={"cat": "${user.category}"},
        ),
    ))
    scenario.add_step(ScenarioStep(
        name="order",
        request=HttpRequest(
            name="o",
            method=HttpMethod.POST,
            url="http://api.com/orders",
            body='{"qty": ${user.quantity}, "pay": "${user.payment_method}", "oid": "ORD${order_id}"}',
        ),
    ))

    params = scenario.parameters.clone()
    vars_d = params.generate()
    ctx = scenario.create_context(user_id="u1")
    ctx.update(vars_d)
    r1 = scenario.steps[0].request.execute(ctx.variables, scenario.default_headers)
    assert "products/1001" in r1.url, f"URL dot path: {r1.url}"
    assert "cat=electronics" in r1.url, f"Query dot path: {r1.url}"
    r2 = scenario.steps[1].request.execute(ctx.variables, scenario.default_headers)
    assert '"qty": 1' in r2.body, f"Body dot path qty: {r2.body}"
    assert '"pay": "credit_card"' in r2.body, f"Body dot path pay: {r2.body}"
    assert '"ORD1000"' in r2.body, f"Counter: {r2.body}"

run_test("Dot path access (user.product_id etc)", test_dot_path)

# ========= 测试5：步骤级统计 =========
def test_step_metrics():
    scenario = Scenario(name="multi")
    for i in range(1, 4):
        scenario.add_step(ScenarioStep(
            name=f"step_{i}",
            request=HttpRequest(name=f"r{i}", method=HttpMethod.GET, url=f"http://t/{i}"),
        ))
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=2,
        concurrency=1,
        qps=15,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()
    m = res.metrics
    assert len(m.by_step) == 3, f"Step count: {len(m.by_step)}"
    for i in range(1, 4):
        key = f"step_{i}"
        assert key in m.by_step, f"Missing step {key}"
        sm = m.by_step[key]
        assert sm.total_requests > 0, f"{key} no requests"
        assert sm.latency.p50_ms > 0, f"{key} no latency"
        assert len(sm.qps_series) > 0, f"{key} no qps series"
    md = m.to_dict()
    assert "by_step" in md
    assert "parameter_stats" in md and "csv_stats" in md

run_test("Step-level metrics (3 steps)", test_step_metrics)

# ========= 测试6：步骤级限速 =========
def test_step_rate():
    scenario = Scenario(name="rate")
    scenario.add_step(ScenarioStep(
        name="fast1",
        request=HttpRequest(name="f1", method=HttpMethod.GET, url="http://t/1"),
    ))
    scenario.add_step(ScenarioStep(
        name="slow",
        request=HttpRequest(name="s", method=HttpMethod.GET, url="http://t/2"),
        qps_limit=5,
    ))
    scenario.add_step(ScenarioStep(
        name="fast2",
        request=HttpRequest(name="f2", method=HttpMethod.GET, url="http://t/3"),
    ))
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=2,
        concurrency=1,
        qps=1000,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()
    m = res.metrics
    slow_qps = m.by_step["slow"].overall_qps
    assert 2 < slow_qps < 9, f"Slow step QPS should be ~5, got {slow_qps}"

run_test("Step-level rate limiting (qps_limit=5)", test_step_rate)

# ========= 测试7：CSV统计进入报告 =========
def test_csv_stats_in_report():
    scenario = Scenario(name="csvrep")
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_p = CsvParameter(name="user", csv_path=csv_path, mode=CsvReadMode.SEQUENTIAL, loop=True)
    c = CounterParameter("seq", start=1)
    scenario.parameters = ParameterSet([csv_p, c])
    scenario.add_step(ScenarioStep(
        name="s",
        request=HttpRequest(name="r", method=HttpMethod.GET, url="http://t/u/${user.product_id}"),
    ))
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=2,
        concurrency=1,
        qps=10,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()
    assert len(res.csv_stats) >= 1
    cs = res.csv_stats[0]
    assert cs["name"] == "user"
    assert cs["total_rows"] == 10
    assert cs["total_call_count"] > 0
    assert len(res.parameter_stats) > 0

run_test("CSV stats in result report", test_csv_stats_in_report)

# ========= 测试8：HTML报告包含步骤和参数 =========
def test_html_report():
    scenario = Scenario(name="html")
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_p = CsvParameter(name="user", csv_path=csv_path, mode=CsvReadMode.SEQUENTIAL, loop=True)
    scenario.parameters = ParameterSet([csv_p])
    scenario.add_step(ScenarioStep(
        name="sa",
        request=HttpRequest(name="ra", method=HttpMethod.GET, url="http://t/a"),
    ))
    scenario.add_step(ScenarioStep(
        name="sb",
        request=HttpRequest(name="rb", method=HttpMethod.POST, url="http://t/b"),
    ))
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=2,
        concurrency=1,
        qps=10,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        hp = f.name
    try:
        HtmlReporter().report(res.metrics, hp, title="Test")
        assert os.path.exists(hp)
        with open(hp, "r", encoding="utf-8") as f:
            c = f.read()
        assert "by step" in c.lower() or "step" in c.lower()
        assert "sa" in c and "sb" in c
        assert "parameter" in c.lower() or "csv" in c.lower()
    finally:
        if os.path.exists(hp):
            os.unlink(hp)

run_test("HTML report includes steps + params", test_html_report)

# ========= 汇总 =========
print()
print("=" * 60)
print(f"Results: {PASS} PASSED, {FAIL} FAILED")
print("=" * 60)
for r in RESULTS:
    if r[0] == "[FAIL]":
        print(r)

sys.exit(0 if FAIL == 0 else 1)
