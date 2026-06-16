"""验证测试脚本 - 验证所有4个修复：
1. QPS限速按单个HTTP请求计数
2. 参数化每次迭代重置 + 内联Parameter展开
3. 默认请求头自动应用到所有步骤
4. 报告统计：HTTP口径与场景口径分离，QPS时间线去重
"""
import sys
import time
import random
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from load_tester import (
    Scenario, ScenarioStep, HttpRequest, HttpMethod,
    ScenarioContext, RequestResult, ResponseData,
    RandomChoiceParameter, RandomIntParameter, CounterParameter,
    SuccessAssertion, StatusCodeAssertion,
    LoadTestEngine, LoadTestConfig, ConstantLoadModel,
    TokenBucketRateLimiter,
)
from load_tester.metrics.sample import Sample, SampleType, SampleStatus
from load_tester.report.console import ConsoleReporter
from load_tester.report.json_report import JsonReporter
from load_tester.report.html_report import HtmlReporter


class RecordingMockExecutor:
    """Mock HTTP执行器，记录所有请求的时间戳、请求头和参数"""
    def __init__(self, latency_range=(0.01, 0.05)):
        self.requests = []
        self.latency_min = latency_range[0]
        self.latency_max = latency_range[1]
        self._lock = None
        try:
            import threading
            self._lock = threading.Lock()
        except ImportError:
            pass

    def __call__(self, request: RequestResult) -> ResponseData:
        import time as _t
        latency = random.uniform(self.latency_min, self.latency_max)
        _t.sleep(latency)

        req_data = {
            "timestamp": _t.time(),
            "name": request.request_name,
            "url": request.url,
            "headers": dict(request.headers),
            "body": request.body,
            "latency": latency,
        }

        if self._lock:
            with self._lock:
                self.requests.append(req_data)
        else:
            self.requests.append(req_data)

        # 模拟响应
        body = {"code": 0, "data": {"status": "ok"}}
        if "login" in request.request_name:
            body["data"]["token"] = f"mock_token_{len(self.requests)}"
        elif "list" in request.request_name:
            body["data"]["products"] = [{"id": 100 + i, "name": f"Product_{i}"} for i in range(5)]
        elif "detail" in request.request_name:
            body["data"]["productId"] = 123
            body["data"]["price"] = 99.99

        return ResponseData(
            status_code=200,
            headers={"Content-Type": "application/json"},
            body=body,
            latency=latency,
            timestamp=_t.time(),
            error=None,
        )


def create_test_scenario() -> Scenario:
    """创建3步请求测试场景，包含默认头和内联Parameter"""
    scenario = Scenario(
        name="3步验证场景",
        description="包含3个HTTP请求的验证场景",
        # 默认请求头 - 应自动应用到所有步骤
        default_headers={
            "User-Agent": "LoadTester/1.0.0 (VerificationTest)",
            "Accept": "application/json",
            "X-Test-Id": "verify-fix-${iteration}",
        },
    )

    # 参数化
    scenario.add_parameter(CounterParameter(name="iteration", start=1, step=1))
    scenario.add_parameter(RandomIntParameter(name="random_page", min_value=1, max_value=100))

    # 步骤1: 商品列表（带内联Parameter的query）
    scenario.add_step(ScenarioStep(
        name="步骤1_商品列表",
        request=HttpRequest(
            name="list_products",
            method=HttpMethod.GET,
            url="/api/products",
            query_params={
                "page": "${random_page}",
                "pageSize": 20,
                # 内联Parameter - 应被展开为具体值
                "category": RandomChoiceParameter(
                    choices=["electronics", "clothing", "books", "home"],
                    weights=[0.4, 0.3, 0.2, 0.1],
                ),
            },
            headers={
                # 自定义头 - 应覆盖默认头
                "X-Step-Name": "list",
            },
        ),
        assertions=[SuccessAssertion()],
    ))

    # 步骤2: 商品详情（带内联Parameter的body）
    scenario.add_step(ScenarioStep(
        name="步骤2_商品详情",
        request=HttpRequest(
            name="product_detail",
            method=HttpMethod.POST,
            url="/api/product/detail",
            body={
                "productId": RandomIntParameter(min_value=1000, max_value=9999),
                # 内联Parameter在嵌套结构中
                "options": {
                    "quantity": RandomIntParameter(min_value=1, max_value=5),
                    "currency": RandomChoiceParameter(choices=["CNY", "USD", "EUR"]),
                },
            },
        ),
        assertions=[SuccessAssertion()],
    ))

    # 步骤3: 加入购物车
    scenario.add_step(ScenarioStep(
        name="步骤3_加入购物车",
        request=HttpRequest(
            name="add_to_cart",
            method=HttpMethod.POST,
            url="/api/cart/add",
            body={
                "productId": 123,
                "quantity": RandomIntParameter(min_value=1, max_value=10),
                "paymentMethod": RandomChoiceParameter(
                    choices=["alipay", "wechat", "card"],
                    weights=[0.5, 0.4, 0.1],
                ),
            },
            headers={
                # 自定义覆盖默认的Accept
                "Accept": "application/json;version=2.0",
            },
        ),
        assertions=[SuccessAssertion()],
    ))

    return scenario


def run_verification_test():
    print("=" * 80)
    print("开始验证测试 - 验证所有4个修复")
    print("=" * 80)

    # 创建mock executor
    mock_executor = RecordingMockExecutor(latency_range=(0.02, 0.04))

    # 创建场景
    scenario = create_test_scenario()
    scenario.prepare_request_urls()

    # 配置：10 QPS，2个worker，运行12秒（前2秒预热，后10秒稳定）
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=12,
        concurrency=2,
        qps=10,
        warmup=2,
        output_console=False,
        output_json=False,
        output_html=False,
    )

    # 运行压测
    print(f"\n运行压测: 3步场景 × 10 QPS × 12秒")
    print("预期: 约 120 HTTP 请求 (10 req/s × 12s)")
    print()

    engine = LoadTestEngine(config, custom_http_executor=mock_executor)
    result = engine.run()

    # 等待metric处理完成
    time.sleep(0.5)

    metrics = result.metrics
    requests = mock_executor.requests

    print(f"\n" + "=" * 80)
    print("验证结果")
    print("=" * 80)

    # ============ 验证1: QPS按HTTP请求计数 ============
    print("\n✅ 验证1: QPS限速按单个HTTP请求计数")
    print(f"   总HTTP请求数: {metrics.throughput.total_requests}")
    print(f"   报告平均QPS: {metrics.throughput.overall_qps:.2f} req/s")
    print(f"   报告峰值QPS: {metrics.throughput.peak_qps:.2f} req/s")

    # 按秒统计实际请求速率
    by_second = defaultdict(int)
    for req in requests:
        ts = int(req["timestamp"])
        by_second[ts] += 1

    if by_second:
        min_ts = min(by_second.keys())
        max_ts = max(by_second.keys())
        print(f"\n   每秒实际请求数 (mock服务端视角):")
        for ts in sorted(by_second.keys()):
            if ts >= min_ts + 2:  # 跳过前2秒预热
                print(f"     {time.strftime('%H:%M:%S', time.localtime(ts))}: {by_second[ts]:3d} req/s")

        # 取稳定区间的平均QPS（跳过前2秒预热）
        stable_counts = [cnt for ts, cnt in by_second.items() if ts >= min_ts + 2]
        if stable_counts:
            avg_stable_qps = sum(stable_counts) / len(stable_counts)
            print(f"   稳定区间平均QPS: {avg_stable_qps:.2f} req/s (目标 10)")

            # 验证接近10 QPS
            if 8 <= avg_stable_qps <= 12:
                print(f"   ✅ PASS: QPS 稳定在目标值附近 (10 ± 20%)")
            else:
                print(f"   ❌ FAIL: QPS 偏离目标值较多")

    # ============ 验证2: 参数化 ============
    print("\n✅ 验证2: 参数化 - 每次迭代重置 + 内联Parameter展开")

    # 检查所有请求的参数是否被正确展开（不是 [object Object] 字符串）
    bad_params = []
    for i, req in enumerate(requests):
        # 检查URL中是否有未展开的Parameter对象
        if "RandomChoiceParameter" in req["url"] or "RandomIntParameter" in str(req["url"]):
            bad_params.append(f"请求{i} URL未展开: {req['url']}")
        # 检查body中是否有未展开的Parameter对象
        body_str = str(req["body"])
        if "Parameter" in body_str and "object at" in body_str:
            bad_params.append(f"请求{i} Body未展开: {body_str[:200]}")

    if bad_params:
        print(f"   ❌ FAIL: 发现 {len(bad_params)} 个参数未展开")
        for bp in bad_params[:5]:
            print(f"     - {bp}")
    else:
        print(f"   ✅ PASS: 所有请求的内联Parameter均已正确展开")

    # 检查每次迭代的参数值是否不同
    # 按迭代分组（每3个请求一轮迭代）
    iteration_values = defaultdict(list)
    for i, req in enumerate(requests):
        iter_num = i // 3 + 1
        if "category" in req["url"]:
            # 从URL中提取category值
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(req["url"])
            qs = parse_qs(parsed.query)
            if "category" in qs:
                iteration_values[iter_num].append(qs["category"][0])

    if iteration_values:
        all_categories = []
        for it, vals in iteration_values.items():
            all_categories.extend(vals)
        unique_cats = set(all_categories)
        print(f"   category参数取值: {sorted(unique_cats)}")
        if len(unique_cats) >= 2:
            print(f"   ✅ PASS: 不同迭代生成了不同的随机参数值")
        else:
            print(f"   ⚠️  警告: category只有一种取值，可能是随机种子问题")

    # 检查paymentMethod是否展开
    payment_methods = set()
    for req in requests:
        body = req.get("body")
        if isinstance(body, dict):
            pm = body.get("paymentMethod")
            if pm:
                payment_methods.add(str(pm))
    if payment_methods:
        print(f"   paymentMethod参数取值: {sorted(payment_methods)}")
        if not any("Parameter" in str(pm) for pm in payment_methods):
            print(f"   ✅ PASS: paymentMethod参数已正确展开为字符串值")
        else:
            print(f"   ❌ FAIL: paymentMethod参数未展开")

    # ============ 验证3: 默认请求头 ============
    print("\n✅ 验证3: 默认请求头自动应用到所有步骤")

    # 检查每个请求是否包含默认头
    default_headers_to_check = ["User-Agent", "Accept"]
    all_have_default = True
    header_coverage = defaultdict(int)

    for i, req in enumerate(requests):
        headers = req["headers"]
        for h in default_headers_to_check:
            if h in headers:
                header_coverage[h] += 1
            else:
                all_have_default = False
                print(f"   ❌ 请求{i} ({req['name']}) 缺少默认头 {h}")

    total_reqs = len(requests)
    for h, cnt in header_coverage.items():
        print(f"   {h}: {cnt}/{total_reqs} 请求包含")

    if all_have_default:
        print(f"   ✅ PASS: 所有 {total_reqs} 个请求都包含默认头 User-Agent 和 Accept")

    # 检查步骤自定义头覆盖默认头
    # 步骤3的Accept应该是 "application/json;version=2.0"
    step3_custom = 0
    step3_override_ok = True
    for req in requests:
        if req["name"] == "add_to_cart":
            step3_custom += 1
            accept = req["headers"].get("Accept", "")
            if "version=2.0" not in accept:
                step3_override_ok = False
                print(f"   ❌ 步骤3的Accept未正确覆盖: {accept}")

    if step3_custom > 0 and step3_override_ok:
        print(f"   ✅ PASS: 步骤3的自定义Accept头正确覆盖了默认值")

    # 检查步骤自定义头 X-Step-Name
    step1_header_ok = True
    for req in requests:
        if req["name"] == "list_products":
            if "X-Step-Name" not in req["headers"]:
                step1_header_ok = False
                print(f"   ❌ 步骤1缺少自定义头 X-Step-Name")
    if step1_header_ok:
        print(f"   ✅ PASS: 步骤1的自定义头 X-Step-Name 已正确设置")

    # ============ 验证4: 报告统计 ============
    print("\n✅ 验证4: 报告统计 - HTTP口径与场景口径分离")
    print(f"   HTTP 请求口径:")
    print(f"     总请求数: {metrics.throughput.total_requests}")
    print(f"     成功数: {metrics.throughput.total_success}")
    print(f"     失败数: {metrics.throughput.total_failures}")
    print(f"     平均QPS: {metrics.throughput.overall_qps:.2f}")

    print(f"   场景迭代口径 (独立统计):")
    print(f"     总迭代数: {metrics.scenario.total_iterations}")
    print(f"     成功迭代: {metrics.scenario.success_iterations}")
    print(f"     失败迭代: {metrics.scenario.failed_iterations}")
    print(f"     平均迭代耗时 P50: {metrics.scenario.latency.p50_ms:.2f}ms")

    # 验证3步场景的迭代数与请求数的关系
    expected_iterations = metrics.throughput.total_requests // 3
    print(f"   预期迭代数 ≈ {expected_iterations} (总请求/3)")
    if abs(metrics.scenario.total_iterations - expected_iterations) <= 2:
        print(f"   ✅ PASS: 迭代数符合预期 (误差 <= 2)")
    else:
        print(f"   ⚠️  迭代数偏差: {metrics.scenario.total_iterations} vs 预期 {expected_iterations}")

    # 验证QPS时间线去重
    qps_series = metrics.throughput.qps_series
    print(f"\n   QPS时间线数据点: {len(qps_series)} 个")

    # 检查同秒是否有重复数据点
    ts_counts = defaultdict(int)
    for ts, qps in qps_series:
        ts_counts[int(ts)] += 1

    dup_seconds = [ts for ts, cnt in ts_counts.items() if cnt > 1]
    if dup_seconds:
        print(f"   ❌ FAIL: 发现重复时间点: {dup_seconds}")
    else:
        print(f"   ✅ PASS: QPS时间线无重复时间点，每秒仅1条数据")

    # 打印QPS时间线
    print(f"\n   QPS时间线详情:")
    for ts, qps in qps_series:
        print(f"     {time.strftime('%H:%M:%S', time.localtime(ts))}: {qps:.2f} req/s")

    # ============ 生成报告 ============
    print("\n✅ 生成报告文件")
    output_dir = Path(__file__).parent / "test_output"
    output_dir.mkdir(exist_ok=True)

    json_path = output_dir / "verification_report.json"
    html_path = output_dir / "verification_report.html"

    JsonReporter().report(metrics, json_path, title="验证测试报告")
    HtmlReporter().report(metrics, html_path, title="验证测试报告")

    print(f"   JSON报告: {json_path}")
    print(f"   HTML报告: {html_path}")

    # 控制台报告
    print("\n" + "=" * 80)
    print("控制台汇总报告")
    print("=" * 80)
    ConsoleReporter().report(metrics)

    print("\n" + "=" * 80)
    print("验证测试完成")
    print("=" * 80)

    return metrics, requests


if __name__ == "__main__":
    try:
        run_verification_test()
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
