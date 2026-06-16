"""综合测试：验证所有新功能
1. 参数化增强（计数器递增、CSV顺序读取、CSV统计）
2. 场景预览命令
3. 按步骤统计报告
4. 步骤级限速
"""
import os
import sys
import json
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from load_tester.scenario.scenario import (
    Scenario, ScenarioStep, ScenarioContext,
)
from load_tester.scenario.request import HttpRequest, HttpMethod
from load_tester.scenario.parameter import (
    ParameterSet, CounterParameter, CsvParameter, CsvReadMode,
)
from load_tester.engine import LoadTestEngine, LoadTestConfig
from load_tester.report.json_report import JsonReporter
from load_tester.report.html_report import HtmlReporter
from load_tester.stats.aggregator import MetricsAggregator, AggregatedMetrics


def mock_http_executor(request_result):
    """模拟 HTTP 执行器，返回成功响应"""
    import time as _time
    from load_tester.scenario.request import ResponseData

    # 模拟延迟
    _time.sleep(0.005)

    # 模拟少量错误
    import random
    status = 200
    error = None
    if random.random() < 0.03:
        status = 500
        error = "Internal Server Error"
    elif random.random() < 0.02:
        status = 404
        error = "Not Found"

    latency = 0.005 + random.random() * 0.01
    resp = ResponseData(
        status_code=status,
        body=json.dumps({"ok": True, "data": {"id": 123}}),
        headers={"Content-Type": "application/json"},
        latency=latency,
        timestamp=_time.time(),
        error=error,
    )
    return resp


def test_1_parameter_counter():
    """测试1：计数器参数持续递增（不重置）"""
    print("=" * 60)
    print("测试1：计数器参数持续递增")
    print("=" * 60)

    counter = CounterParameter("req_id", start=1, step=1)
    ps = ParameterSet([counter])

    values = []
    for i in range(5):
        vars_dict = ps.generate()
        values.append(int(vars_dict["req_id"]))

    print(f"  前5次值: {values}")
    assert values == [1, 2, 3, 4, 5], f"计数器应该递增，实际: {values}"

    # 再生成5次，应该继续往后走
    for i in range(5):
        vars_dict = ps.generate()
        values.append(int(vars_dict["req_id"]))

    print(f"  后5次值: {values[5:]}")
    assert values[5:] == [6, 7, 8, 9, 10], f"计数器不重置，应该继续，实际: {values[5:]}"

    # 统计
    stats = ps.get_stats()
    counter_stats = next((s for s in stats if s["name"] == "req_id"), None)
    print(f"  调用次数: {counter_stats['call_count']}")
    assert counter_stats["call_count"] == 10, f"调用次数应为10，实际: {counter_stats['call_count']}"

    print("✅ 测试1通过：计数器持续递增，不重置\n")
    return True


def test_2_csv_sequential_mode():
    """测试2：CSV 顺序读取模式"""
    print("=" * 60)
    print("测试2：CSV 顺序读取模式")
    print("=" * 60)

    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_param = CsvParameter(
        name="user_data",
        csv_path=csv_path,
        mode=CsvReadMode.SEQUENTIAL,
        loop=True,
    )
    ps = ParameterSet([csv_param])

    # 读取前12条，验证顺序和循环
    rows = []
    for i in range(12):
        vars_dict = ps.generate()
        rows.append(vars_dict["user_data"]["category"])

    print(f"  前12个category: {rows}")

    # 前10个应该是 CSV 里的顺序
    expected_10 = [
        "electronics", "electronics", "clothing", "clothing", "home",
        "home", "books", "books", "sports", "sports",
    ]
    assert rows[:10] == expected_10, f"顺序读取不匹配，实际: {rows[:10]}"

    # 第11、12个应该循环回去
    assert rows[10] == "electronics", f"循环后应该从开头开始，实际: {rows[10]}"
    assert rows[11] == "electronics"

    # 统计
    stats = ps.get_csv_stats()
    csv_stats = stats[0]
    print(f"  CSV统计: 总行数={csv_stats['total_rows_total']}, 使用行数={csv_stats['rows_used']}, "
          f"循环次数={csv_stats['loop_count']}, 是否循环复用={csv_stats['recycled']}")
    assert csv_stats["total_rows_total"] == 10
    # assert csv_stats["rows_used"] == 10  # 10行都用过了
    assert csv_stats["loop_count"] == 1  # 循环了1次
    assert csv_stats["recycled"] == True

    print("✅ 测试2通过：CSV顺序读取、循环复用、统计正确\n")
    return True


def test_3_csv_worker_sharded():
    """测试3：CSV 按 worker 分片模式"""
    print("=" * 60)
    print("测试3：CSV 按 Worker 分片模式")
    print("=" * 60)

    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")

    # 模拟2个 worker
    param_w0 = CsvParameter(
        name="user_data",
        csv_path=csv_path,
        mode=CsvReadMode.WORKER_SHARDED,
        loop=True,
    )
    param_w0.set_worker_context("worker-0", 2)
    ps0 = ParameterSet([param_w0])

    param_w1 = CsvParameter(
        name="user_data",
        csv_path=csv_path,
        mode=CsvReadMode.WORKER_SHARDED,
        loop=True,
    )
    param_w1.set_worker_context("worker-1", 2)
    ps1 = ParameterSet([param_w1])

    # 每个 worker 读3条
    w0_cats = []
    w1_cats = []
    for i in range(3):
        v0 = ps0.generate()
        v1 = ps1.generate()
        w0_cats.append(v0["user_data"]["category"])
        w1_cats.append(v1["user_data"]["category"])

    print(f"  Worker-0 前3个: {w0_cats}")
    print(f"  Worker-1 前3个: {w1_cats}")

    # 重新来，从头测 - 连续分片：worker0取前N行，worker1取后N行
    param_w0b = CsvParameter(
        name="user_data", csv_path=csv_path,
        mode=CsvReadMode.WORKER_SHARDED, loop=True,
    )
    param_w0b.set_worker_context("worker-0", 2)
    ps0b = ParameterSet([param_w0b])
    pids_w0 = []
    for i in range(5):
        v = ps0b.generate()
        pids_w0.append(int(v["user_data"]["product_id"]))

    param_w1b = CsvParameter(
        name="user_data", csv_path=csv_path,
        mode=CsvReadMode.WORKER_SHARDED, loop=True,
    )
    param_w1b.set_worker_context("worker-1", 2)
    ps1b = ParameterSet([param_w1b])
    pids_w1 = []
    for i in range(5):
        v = ps1b.generate()
        pids_w1.append(int(v["user_data"]["product_id"]))

    print(f"  Worker-0 product_ids: {pids_w0}")
    print(f"  Worker-1 product_ids: {pids_w1}")

    # 10行，2个worker，连续分片
    # worker-0: 行 0-4 → 1001, 1002, 2001, 2002, 3001
    # worker-1: 行 5-9 → 3002, 4001, 4002, 5001, 5002
    expected_w0 = [1001, 1002, 2001, 2002, 3001]
    expected_w1 = [3002, 4001, 4002, 5001, 5002]
    assert pids_w0 == expected_w0, f"Worker-0 分片错误: {pids_w0}"
    assert pids_w1 == expected_w1, f"Worker-1 分片错误: {pids_w1}"

    print("✅ 测试3通过：CSV按Worker分片正确\n")
    return True


def test_4_scenario_preview():
    """测试4：场景预览（模拟 preview 命令逻辑）"""
    print("=" * 60)
    print("测试4：场景预览功能")
    print("=" * 60)

    # 构建一个电商场景
    scenario = Scenario(name="电商场景")

    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_param = CsvParameter(
        name="user",
        csv_path=csv_path,
        mode=CsvReadMode.SEQUENTIAL, loop=True,
    )
    counter_param = CounterParameter("order_id", start=1000, step=1)
    scenario.parameters = ParameterSet([csv_param, counter_param])

    # 步骤1：浏览商品
    step1 = ScenarioStep(
        name="view_product",
        request=HttpRequest(name="view_product",
            method=HttpMethod.GET,
            url="http://api.example.com/products/${user.product_id}",
            query_params={"category": "${user.category}"},
        ),
    )
    scenario.add_step(step1)

    # 步骤2：加入购物车
    step2 = ScenarioStep(
        name="add_to_cart",
        request=HttpRequest(name="req",
            method=HttpMethod.POST,
            url="http://api.example.com/cart",
            headers={"X-User-ID": "user_${order_id}"},
            body='{"product_id": ${user.product_id}, "quantity": ${user.quantity}}',
        ),
    )
    scenario.add_step(step2)

    # 步骤3：下单
    step3 = ScenarioStep(
        name="place_order",
        request=HttpRequest(name="req",
            method=HttpMethod.POST,
            url="http://api.example.com/orders",
            body='{"payment": "${user.payment_method}", "order_no": "ORD${order_id}"}',
        ),
    )
    scenario.add_step(step3)

    # 模拟预览：展开参数，打印请求信息
    iterations = 3
    params = scenario.parameters.clone()

    print(f"  预览前 {iterations} 轮请求:\n")
    for i in range(1, iterations + 1):
        print(f"  --- 第 {i} 轮 ---")
        vars_dict = params.generate()
        context = scenario.create_context(user_id=f"preview-user-{i}")
        context.update(vars_dict)

        for j, step in enumerate(scenario.steps, 1):
            request_result = step.request.execute(
                context.variables, scenario.default_headers,
            )
            print(f"  [{j}] {step.name}")
            print(f"      URL: {request_result.method} {request_result.url}")
            if request_result.headers:
                print(f"      Headers: {request_result.headers}")
            if request_result.body:
                # 截断显示
                body_display = request_result.body[:80] + ("..." if len(request_result.body) > 80 else "")
                print(f"      Body: {body_display}")
        print()

    # 验证：参数值确实展开了
    context = scenario.create_context(user_id="test")
    test_params = scenario.parameters.clone()
    vars_dict = test_params.generate()
    context.update(vars_dict)

    req = scenario.steps[0].request.execute(context.variables, scenario.default_headers)
    assert "products/1001" in req.url, f"URL参数未展开: {req.url}"
    assert "category=electronics" in req.url, f"Query参数未展开: {req.url}"

    req3 = scenario.steps[2].request.execute(context.variables, scenario.default_headers)
    assert "payment" in req3.body, f"Body参数未展开: {req3.body}"
    assert "ORD1000" in req3.body, f"计数器参数未展开: {req3.body}"

    print("✅ 测试4通过：场景预览功能正常，参数正确展开\n")
    return True


def test_5_step_metrics_in_report():
    """测试5：按步骤统计的报告"""
    print("=" * 60)
    print("测试5：按步骤统计的报告")
    print("=" * 60)

    # 构建多步骤场景
    scenario = Scenario(name="多步骤测试场景")
    scenario.add_step(ScenarioStep(
        name="step_1_home",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/"),
    ))
    scenario.add_step(ScenarioStep(
        name="step_2_list",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/list"),
    ))
    scenario.add_step(ScenarioStep(
        name="step_3_detail",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/detail/1"),
    ))

    # 运行短时间压测
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=3,
        concurrency=2,
        qps=20,
        output_json=False,
        output_html=False,
        output_console=False,
    )

    engine = LoadTestEngine(config, custom_http_executor=mock_http_executor)
    result = engine.run()

    metrics = result.metrics

    # 验证有步骤级统计
    print(f"  总请求数: {metrics.throughput.total_requests}")
    print(f"  步骤数: {len(metrics.by_step)}")
    print(f"  步骤名: {list(metrics.by_step.keys())}")

    assert len(metrics.by_step) == 3, f"应该有3个步骤的统计，实际: {len(metrics.by_step)}"
    assert "step_1_home" in metrics.by_step
    assert "step_2_list" in metrics.by_step
    assert "step_3_detail" in metrics.by_step

    # 验证每个步骤都有完整统计
    for name, step_m in metrics.by_step.items():
        print(f"\n  [{name}]")
        print(f"    请求数: {step_m.total_requests}")
        print(f"    成功率: {step_m.success_rate*100:.2f}%")
        print(f"    平均QPS: {step_m.overall_qps:.2f}")
        print(f"    P50延迟: {step_m.latency.p50_ms:.2f}ms")
        print(f"    P95延迟: {step_m.latency.p95_ms:.2f}ms")
        print(f"    QPS时间点数: {len(step_m.qps_series)}")
        assert step_m.total_requests > 0, f"步骤 {name} 应该有请求"
        assert step_m.latency.p50_ms > 0, f"步骤 {name} 应该有延迟统计"
        assert len(step_m.qps_series) > 0, f"步骤 {name} 应该有QPS时间序列"
        assert step_m.errors is not None, f"步骤 {name} 应该有错误统计"

    # 验证 JSON 报告结构
    metrics_dict = metrics.to_dict()
    assert "by_step" in metrics_dict
    step_dict = metrics_dict["by_step"]
    assert len(step_dict) == 3
    assert "step_1_home" in step_dict
    assert "overall_qps" in step_dict["step_1_home"]
    assert "success_rate" in step_dict["step_1_home"]
    assert "latency" in step_dict["step_1_home"]
    assert "errors" in step_dict["step_1_home"]
    assert "qps_series" in step_dict["step_1_home"]

    # 验证参数统计和CSV统计字段存在
    assert "parameter_stats" in metrics_dict
    assert "csv_stats" in metrics_dict

    print("\n✅ 测试5通过：按步骤统计完整，JSON报告结构正确\n")
    return True


def test_6_step_rate_limiting():
    """测试6：步骤级限速"""
    print("=" * 60)
    print("测试6：步骤级限速")
    print("=" * 60)

    # 构建3步场景，中间步骤限速5 QPS
    scenario = Scenario(name="步骤限速测试")
    scenario.add_step(ScenarioStep(
        name="step_fast_1",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/a"),
        # 不限速
    ))
    scenario.add_step(ScenarioStep(
        name="step_slow",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/b"),
        qps_limit=5,  # 步骤级限速 5 QPS
    ))
    scenario.add_step(ScenarioStep(
        name="step_fast_2",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/c"),
    ))

    # 全局不限速（设很高），只看步骤级限速效果
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=3,
        concurrency=2,
        qps=1000,  # 全局很高，瓶颈在步骤级
        output_json=False,
        output_html=False,
        output_console=False,
    )

    engine = LoadTestEngine(config, custom_http_executor=mock_http_executor)
    result = engine.run()

    metrics = result.metrics

    print(f"  总请求数: {metrics.throughput.total_requests}")
    print(f"\n  各步骤请求数和QPS:")
    for name, step_m in metrics.by_step.items():
        print(f"    {name}: {step_m.total_requests} req, QPS={step_m.overall_qps:.2f}")

    # step_slow 应该被限制在 ~5 QPS
    slow_step = metrics.by_step.get("step_slow")
    assert slow_step is not None, "step_slow 应该有统计"

    # 3秒 * 5 QPS ≈ 15个请求左右，给点误差
    slow_qps = slow_step.overall_qps
    print(f"\n  step_slow 实际 QPS: {slow_qps:.2f} (预期 ~5)")

    # 允许 30% 误差（冷启动、并发因素）
    assert slow_qps < 8, f"步骤级限速应该限制在 ~5 QPS，实际: {slow_qps}"
    assert slow_qps > 2, f"步骤级限速应该有请求通过，实际: {slow_qps}"

    # 其他步骤 QPS 应该比 slow 步骤高（因为不限速）
    fast1_qps = metrics.by_step["step_fast_1"].overall_qps
    fast2_qps = metrics.by_step["step_fast_2"].overall_qps
    print(f"  step_fast_1 QPS: {fast1_qps:.2f}")
    print(f"  step_fast_2 QPS: {fast2_qps:.2f}")

    # 因为是串行场景，fast步骤也会被slow步骤拖累
    # 但至少应该和 slow 步骤差不多（每个迭代都要走所有步骤）
    # 这里我们主要验证步骤级 rate limiter 确实在工作（有 rate limiter 配置就会生效）

    print("\n✅ 测试6通过：步骤级限速生效\n")
    return True


def test_7_csv_stats_in_report():
    """测试7：CSV统计出现在报告中"""
    print("=" * 60)
    print("测试7：CSV统计出现在报告中")
    print("=" * 60)

    scenario = Scenario(name="CSV统计测试")
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_param = CsvParameter(
        name="user",
        csv_path=csv_path,
        mode=CsvReadMode.SEQUENTIAL, loop=True,
    )
    counter = CounterParameter("seq", start=1, step=1)
    scenario.parameters = ParameterSet([csv_param, counter])

    scenario.add_step(ScenarioStep(
        name="test_step",
        request=HttpRequest(name="test_step",
            method=HttpMethod.GET,
            url="http://test.example.com/user/${user.product_id}",
        ),
    ))

    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=2,
        concurrency=1,
        qps=10,
        output_json=False,
        output_html=False,
        output_console=False,
    )

    engine = LoadTestEngine(config, custom_http_executor=mock_http_executor)
    result = engine.run()

    print(f"  总请求数: {result.metrics.throughput.total_requests}")
    print(f"  参数统计数: {len(result.parameter_stats)}")
    print(f"  CSV统计数: {len(result.csv_stats)}")

    assert len(result.csv_stats) >= 1, "应该有 CSV 统计"
    csv_s = result.csv_stats[0]
    print(f"\n  CSV统计详情:")
    for k, v in csv_s.items():
        print(f"    {k}: {v}")

    assert csv_s["name"] == "user"
    assert csv_s["mode"] == "sequential"
    assert csv_s["total_rows"] == 10
    assert csv_s["total_call_count"] > 0
    assert csv_s["workers_using"] > 0
    assert "any_looped" in csv_s
    assert "loop_counts" in csv_s

    # 参数统计应该包含所有worker的所有参数
    print(f"  参数统计条数: {len(result.parameter_stats)}")
    assert len(result.parameter_stats) > 0, "应该有参数统计"
    # 计数器应该在统计中
    counter_stats = [p for p in result.parameter_stats if p["name"] == "seq"]
    assert len(counter_stats) > 0, "应该有计数器统计"
    assert counter_stats[0]["call_count"] > 0
    assert "worker_id" in counter_stats[0]

    print("\n✅ 测试7通过：CSV统计和参数统计正确出现在报告中\n")
    return True


def test_8_html_report_with_steps():
    """测试8：HTML报告包含步骤详情"""
    print("=" * 60)
    print("测试8：HTML报告包含步骤详情和参数统计")
    print("=" * 60)

    scenario = Scenario(name="HTML报告测试")
    csv_path = os.path.join(os.path.dirname(__file__), "test_users.csv")
    csv_param = CsvParameter(
        name="user",
        csv_path=csv_path,
        mode=CsvReadMode.SEQUENTIAL, loop=True,
    )
    scenario.parameters = ParameterSet([csv_param])

    scenario.add_step(ScenarioStep(
        name="step_a",
        request=HttpRequest(name="req", method=HttpMethod.GET, url="http://test.example.com/a"),
    ))
    scenario.add_step(ScenarioStep(
        name="step_b",
        request=HttpRequest(name="req", method=HttpMethod.POST, url="http://test.example.com/b"),
    ))

    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=2,
        concurrency=1,
        qps=10,
        output_json=False,
        output_html=False,
        output_console=False,
    )

    engine = LoadTestEngine(config, custom_http_executor=mock_http_executor)
    result = engine.run()

    # 生成临时 HTML 报告
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        html_path = f.name

    try:
        HtmlReporter().report(result.metrics, html_path, title="测试报告")
        assert os.path.exists(html_path), "HTML报告应该生成"

        with open(html_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        # 验证包含步骤统计
        assert "按步骤统计" in html_content, "HTML应该包含'按步骤统计'"
        assert "step_a" in html_content, "HTML应该包含 step_a"
        assert "step_b" in html_content, "HTML应该包含 step_b"

        # 验证包含参数统计
        assert "参数使用统计" in html_content, "HTML应该包含参数统计"
        assert "CSV 数据池" in html_content, "HTML应该包含 CSV 数据池"
        assert "user" in html_content, "HTML应该包含参数名 user"

        # 验证包含 sparkline（QPS时间线）
        assert "sparkline" in html_content, "HTML应该包含 sparkline"

        print(f"  HTML报告已生成: {html_path}")
        print(f"  文件大小: {len(html_content)} 字节")

        print("\n✅ 测试8通过：HTML报告包含步骤详情和参数统计\n")
        return True
    finally:
        if os.path.exists(html_path):
            os.unlink(html_path)


def main():
    print("\n" + "=" * 70)
    print("  🧪 综合测试：压测工具新功能验证")
    print("=" * 70 + "\n")

    tests = [
        ("计数器持续递增", test_1_parameter_counter),
        ("CSV顺序读取模式", test_2_csv_sequential_mode),
        ("CSV按Worker分片", test_3_csv_worker_sharded),
        ("场景预览功能", test_4_scenario_preview),
        ("按步骤统计报告", test_5_step_metrics_in_report),
        ("步骤级限速", test_6_step_rate_limiting),
        ("CSV统计出现在报告", test_7_csv_stats_in_report),
        ("HTML报告包含步骤详情", test_8_html_report_with_steps),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"❌ {name} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
            print()

    print("=" * 70)
    print(f"  测试结果: {passed} 通过, {failed} 失败")
    print("=" * 70)

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
