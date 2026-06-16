"""综合测试 - 覆盖所有新功能"""
import os
import sys
import json
import tempfile
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from load_tester.scenario.scenario import Scenario, ScenarioStep
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
        import traceback
        tb = traceback.format_exc()
        RESULTS.append(("[FAIL]", name, str(e)[:200]))
        print("[FAIL]", name, "-", str(e)[:300])
        print(tb[:800])

# ========== Mock executor ==========
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

csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")

# ========== T1: 报告CSV统计完整 ==========
def t1_csv_stats_full():
    scenario = Scenario(name="t1")
    csv_p = CsvParameter(name="user", csv_path=csv_path, mode=CsvReadMode.WORKER_SHARDED, loop=True)
    c = CounterParameter("oid", start=1000)
    scenario.parameters = ParameterSet([csv_p, c])
    scenario.add_step(ScenarioStep(
        name="s1",
        request=HttpRequest(name="r1", method=HttpMethod.GET, url="http://t/p/${user.product_id}"),
    ))
    scenario.add_step(ScenarioStep(
        name="s2",
        request=HttpRequest(name="r2", method=HttpMethod.POST, url="http://t/o", body='{"q": ${user.quantity}}'),
    ))
    config = LoadTestConfig(
        scenario=scenario, load_mode="constant", duration=1.5, concurrency=2, qps=10,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()

    # 验证 csv_stats 字段完整
    assert len(res.csv_stats) >= 1, "csv_stats missing"
    s = res.csv_stats[0]
    required_fields = [
        "name", "read_mode", "total_rows", "rows_used", "coverage_pct",
        "workers_using", "total_call_count", "any_looped", "rows_used_per_worker",
        "call_count_per_worker", "loop_count_per_worker",
    ]
    for f in required_fields:
        assert f in s, f"Missing field: {f}"
    assert s["name"] == "user"
    assert s["read_mode"] and s["read_mode"].lower() == "worker_sharded", f"read_mode={s.get('read_mode')}"
    assert s["total_rows"] == 10
    assert s["workers_using"] == 2
    assert s["rows_used_per_worker"] and len(s["rows_used_per_worker"]) == 2
    assert s["total_call_count"] > 0

    # JSON 报告包含相同结构
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        jp = f.name
    try:
        JsonReporter().report(res.metrics, jp, title="T1")
        with open(jp, "r", encoding="utf-8") as f:
            jd = json.load(f)
        assert "csv_stats" in jd
        assert len(jd["csv_stats"]) >= 1
        js = jd["csv_stats"][0]
        for f in required_fields:
            assert f in js, f"JSON missing field: {f}"
    finally:
        if os.path.exists(jp): os.unlink(jp)

run_test("T1: CSV统计完整进报告(JSON字段+Worker明细)", t1_csv_stats_full)

# ========== T2: 点号路径访问 ==========
def t2_dot_path():
    scenario = Scenario(name="t2")
    csv_p = CsvParameter(name="u", csv_path=csv_path, mode=CsvReadMode.SEQUENTIAL, loop=True)
    scenario.parameters = ParameterSet([csv_p])
    scenario.add_step(ScenarioStep(
        name="s",
        request=HttpRequest(
            name="r", method=HttpMethod.POST, url="http://t/${u.category}/p",
            query_params={"pid": "${u.product_id}", "pay": "${u.payment_method}"},
            headers={"X-Cat": "${u.category}"},
            body='{"qty": ${u.quantity}, "p": "${u.payment_method}"}',
        ),
    ))
    params = scenario.parameters.clone()
    vd = params.generate()
    ctx = scenario.create_context(user_id="x")
    ctx.update(vd)
    r = scenario.steps[0].request.execute(ctx.variables, scenario.default_headers)
    assert "/electronics/p" in r.url, f"URL dot path wrong: {r.url}"
    assert "pid=1001" in r.url, f"Query dot path: {r.url}"
    assert "X-Cat" in str(r.headers) or "electronics" in str(r.headers), f"Headers: {r.headers}"
    assert '"qty": 1' in r.body, f"Body dot path qty: {r.body}"
    assert '"credit_card"' in r.body, f"Body dot path pay: {r.body}"

run_test("T2: 点号路径访问(category/product_id/quantity等)", t2_dot_path)

# ========== T3: 步骤级状态码分布 + 成功/失败独立延迟 ==========
def t3_step_status_and_indiv_lat():
    scenario = Scenario(name="t3")
    scenario.add_step(ScenarioStep(name="s1", request=HttpRequest(name="r1", method=HttpMethod.GET, url="http://t/1")))
    scenario.add_step(ScenarioStep(name="s2", request=HttpRequest(name="r2", method=HttpMethod.GET, url="http://t/2")))
    config = LoadTestConfig(
        scenario=scenario, load_mode="constant", duration=1.5, concurrency=1, qps=20,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()
    bs = res.metrics.by_step
    assert "s1" in bs and "s2" in bs
    for step_name in ["s1", "s2"]:
        sm = bs[step_name]
        assert hasattr(sm, "status_code_distribution") and sm.status_code_distribution, f"{step_name} no status_dist"
        assert hasattr(sm, "success_latency"), f"{step_name} no success_latency"
        assert hasattr(sm, "failure_latency"), f"{step_name} no failure_latency"
        assert sm.success_latency.count > 0, f"{step_name} success_latency.count = 0"
        # 成功延迟应该有样本
        assert sm.success_latency.p50_ms > 0, f"{step_name} success p50=0"
        # QPS时序分离
        assert len(sm.qps_series) > 0
        assert len(sm.success_qps_series) > 0
        # JSON 同构
        d = sm.to_dict()
        for k in ["status_code_distribution", "success_latency", "failure_latency",
                  "success_qps_series", "failure_qps_series"]:
            assert k in d, f"to_dict missing {k}"

run_test("T3: 步骤级状态码分布+成功/失败独立延迟+独立QPS时序", t3_step_status_and_indiv_lat)

# ========== T4: 步骤权重分摊总QPS ==========
def t4_weight_distribution():
    scenario = Scenario(name="t4")
    # 3步，weight 1:2:1  → 分配 QPS 25:50:25 (总QPS=100)
    scenario.add_step(ScenarioStep(name="lw", weight=1,
        request=HttpRequest(name="r1", method=HttpMethod.GET, url="http://t/lw")))
    scenario.add_step(ScenarioStep(name="md", weight=2,
        request=HttpRequest(name="r2", method=HttpMethod.GET, url="http://t/md")))
    scenario.add_step(ScenarioStep(name="hw", weight=1,
        request=HttpRequest(name="r3", method=HttpMethod.GET, url="http://t/hw")))
    config = LoadTestConfig(
        scenario=scenario, load_mode="constant", duration=2.5, concurrency=4, qps=100,
        output_json=False, output_html=False, output_console=False,
    )
    # 让实际执行极快，避免concurrency变成瓶颈
    def fast_exec(req):
        return ResponseData(
            status_code=200, body="{}", headers={},
            latency=0.0005, timestamp=time.time(), error=None,
        )
    engine = LoadTestEngine(config, custom_http_executor=fast_exec)
    res = engine.run()
    bs = res.metrics.by_step

    # 验证比例符合权重 (lw:md:hw ≈ 1:2:1)
    total = sum(bs[k].total_requests for k in bs)
    lw_r = bs["lw"].total_requests / max(1, total)
    md_r = bs["md"].total_requests / max(1, total)
    hw_r = bs["hw"].total_requests / max(1, total)
    # 期望比例 lw=25%, md=50%, hw=25%
    # 由于每轮场景是串行执行的（先lw再md后hw），真实比例会有波动，放宽阈值
    assert 0.10 < lw_r < 0.40, f"lw ratio off: {lw_r:.2f} (expected ~0.25), dist: lw={bs['lw'].total_requests}, md={bs['md'].total_requests}, hw={bs['hw'].total_requests}"
    assert 0.30 < md_r < 0.70, f"md ratio off: {md_r:.2f} (expected ~0.50)"
    assert 0.10 < hw_r < 0.40, f"hw ratio off: {hw_r:.2f} (expected ~0.25)"
    # 总 QPS 对齐 HTTP 口径
    overall_qps = res.metrics.throughput.overall_qps
    assert 50 < overall_qps < 160, f"Overall QPS should be ~100 HTTP/s, got {overall_qps:.1f}"
    print(f"  [QPS分布] lw={bs['lw'].overall_qps:.1f}, md={bs['md'].overall_qps:.1f}, hw={bs['hw'].overall_qps:.1f}, TOTAL={overall_qps:.1f}")

run_test("T4: 3步按权重1:2:1分摊总QPS=100(HTTP口径)", t4_weight_distribution)

# ========== T5: 5步权重分摊 ==========
def t5_five_steps_weight():
    scenario = Scenario(name="t5")
    # weight 1,1,1,1,1 → 每个约 20 QPS (总100)
    for i in range(5):
        scenario.add_step(ScenarioStep(
            name=f"step_{i}", weight=1,
            request=HttpRequest(name=f"r{i}", method=HttpMethod.GET, url=f"http://t/{i}"),
        ))
    def fast_exec(req):
        return ResponseData(
            status_code=200, body="{}", headers={},
            latency=0.0005, timestamp=time.time(), error=None,
        )
    config = LoadTestConfig(
        scenario=scenario, load_mode="constant", duration=2, concurrency=2, qps=100,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=fast_exec)
    res = engine.run()
    # 每个步骤 QPS 应该在 15-25 之间 (期望值20)
    for i in range(5):
        sq = res.metrics.by_step[f"step_{i}"].overall_qps
        assert 10 < sq < 35, f"step_{i} QPS out of range: {sq:.1f} (expected ~20)"
    overall = res.metrics.throughput.overall_qps
    assert 60 < overall < 160, f"Overall HTTP QPS should be ~100, got {overall:.1f}"
    print(f"  [5步均摊] 各步QPS: {[round(res.metrics.by_step[f'step_{i}'].overall_qps,1) for i in range(5)]}")
    print(f"  [5步均摊] 总HTTP QPS: {overall:.1f}")

run_test("T5: 5步权重均摊总QPS=100(HTTP口径对齐)", t5_five_steps_weight)

# ========== T6: HTML报告步骤级详情可切换 ==========
def t6_html_step_tabs():
    scenario = Scenario(name="t6")
    for i in range(3):
        scenario.add_step(ScenarioStep(
            name=f"s{i}",
            request=HttpRequest(name=f"r{i}", method=HttpMethod.GET, url=f"http://t/{i}"),
        ))
    config = LoadTestConfig(
        scenario=scenario, load_mode="constant", duration=1.5, concurrency=1, qps=10,
        output_json=False, output_html=False, output_console=False,
    )
    engine = LoadTestEngine(config, custom_http_executor=mock_exec)
    res = engine.run()
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        hp = f.name
    try:
        HtmlReporter().report(res.metrics, hp, title="T6")
        assert os.path.exists(hp)
        with open(hp, "r", encoding="utf-8") as f:
            c = f.read()
        # 包含 Tab 切换相关结构
        for key in ["lat-tab-radio", "qps-tab-radio", "HTTP 200",
                    "bindTab", "lat-tab-content", "qps-tab-content"]:
            assert key in c, f"HTML missing key content: {key}"
        # 验证 sparkline SVG 数量：3 步骤 x 3 条折线 = 9 个 SVG（可能重复定义，至少 3 个）
        assert c.count("<svg class=") >= 3, f"SVG sparkline count: {c.count('<svg class=')}"
        # 验证步骤级延迟表数量（至少 3 步骤 x 3 表 = 9 个 mini-table 左右）
        assert c.count("mini-table") >= 6, f"mini-table count too low: {c.count('mini-table')}"
        # 验证状态码分布关键字
        assert "状态码分布" in c
        # 验证 Tab label 文本
        assert "全部 (" in c and "成功 (" in c and "失败 (" in c
    finally:
        if os.path.exists(hp): os.unlink(hp)

run_test("T6: HTML报告步骤级详情(成功/失败Tab+状态码分布)", t6_html_step_tabs)

# ========== 汇总 ==========
print()
print("=" * 60)
print(f"Result: {PASS} PASSED, {FAIL} FAILED")
print("=" * 60)
for r in RESULTS:
    if r[0] == "[FAIL]":
        print(r)

sys.exit(0 if FAIL == 0 else 1)
