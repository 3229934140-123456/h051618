"""完整集成测试：使用 mock HTTP 执行器验证端到端流程"""
import sys, os, random, time
sys.path.insert(0, os.path.dirname(__file__))

from load_tester import (
    LoadTestEngine, LoadTestConfig,
    Scenario, ScenarioStep, HttpRequest, HttpMethod,
    SuccessAssertion, JsonPathAssertion, LatencyThresholdAssertion,
    StatusCodeInAssertion,
    RandomIntParameter, RandomStringParameter,
    Extractor,
)
from load_tester.scenario.request import RequestResult, ResponseData


def mock_http_executor(req: RequestResult) -> ResponseData:
    """Mock HTTP 执行器：根据URL返回不同的模拟响应"""
    start = time.perf_counter()
    random.seed(hash(req.url) ^ int(time.time() * 1000000) & 0xFFFFFFFF)

    # 模拟处理延迟（正态分布 ~50ms，偶尔长尾）
    base_latency = 0.03 + random.random() * 0.07  # 30-100ms
    if random.random() < 0.02:  # 2% 的慢请求
        base_latency += 0.1 + random.random() * 0.5

    # 模拟网络开销
    time.sleep(base_latency * 0.1)  # 少量真实睡眠，其余用计算模拟

    status_code = 200
    error = None
    body = '{}'

    # 不同端点的不同响应
    url = req.url.lower()
    if '/home' in url:
        body = '{"code":0,"banner":["banner1","banner2"],"recommend":[]}'
    elif '/login' in url or '/auth' in url:
        # 偶尔登录失败
        if random.random() < 0.01:
            status_code = 401
            body = '{"code":401,"message":"Invalid credentials"}'
        else:
            token = 'fake_token_' + ''.join(random.choices('abcdef0123456789', k=32))
            user_id = random.randint(10000, 99999)
            body = f'{{"code":0,"data":{{"token":"{token}","userId":{user_id}}}}}'
    elif '/products' in url and req.method == 'GET' and '${first_product_id}' not in req.url:
        # 商品列表
        n = 10 + random.randint(0, 10)
        items = ','.join(
            f'{{"id":{1000+i},"name":"Product {i}","price":{9.99 + i * 5:.2f}}}'
            for i in range(n)
        )
        body = f'{{"code":0,"items":[{items}],"total":{n}}}'
    elif '/products' in url:
        # 商品详情
        pid = url.rstrip('/').split('/')[-1]
        price = round(random.random() * 500 + 10, 2)
        body = f'{{"code":0,"data":{{"id":{pid},"name":"Product {pid}","price":{price},"stock":50}}}}'
    elif '/cart' in url:
        if random.random() < 0.03:
            status_code = 409
            body = '{"code":409,"message":"Out of stock"}'
        else:
            body = '{"code":0,"success":true,"cartCount":2}'
    elif '/orders' in url or '/checkout' in url:
        r = random.random()
        if r < 0.02:
            status_code = 429
            body = '{"code":429,"message":"Too many requests"}'
        elif r < 0.05:
            status_code = 500
            body = '{"code":500,"message":"Internal server error"}'
        else:
            oid = random.randint(100000, 999999)
            body = f'{{"code":0,"success":true,"data":{{"orderId":{oid}}}}}'
    elif '/health' in url:
        status_code = 200
        body = '{"status":"ok"}'
    else:
        body = '{"code":0,"message":"default"}'

    # 偶尔网络超时
    if random.random() < 0.005:
        time.sleep(0.5)
        error = "Connection timeout: simulated timeout"
        status_code = 0

    latency = time.perf_counter() - start
    return ResponseData(
        status_code=status_code,
        headers={"Content-Type": "application/json", "Server": "Mock/1.0"},
        body=body,
        latency=latency,
        timestamp=time.time(),
        error=error,
    )


def build_test_scenario() -> Scenario:
    scenario = Scenario(
        name="Mock电商集成测试",
        description="完整流程测试：登录→列表→详情→加购→下单",
        base_url="http://mock-server.local",
        iteration_pause=0.1,
    )
    scenario.add_parameter(RandomStringParameter("username", 5, 10, prefix="testuser_"))
    scenario.add_parameter(RandomIntParameter("page", 1, 100))
    scenario.add_parameter(RandomStringParameter("trace_id", 32, 32, charset="abcdef0123456789"))

    # 步骤1: 登录
    scenario.add_step(ScenarioStep(
        name="登录",
        request=HttpRequest(
            name="login",
            method=HttpMethod.POST,
            url="/api/auth/login",
            body={"username": "${username}", "password": "pass123456"},
            content_type="application/json",
            headers={"X-Trace-Id": "${trace_id}"},
        ),
        assertions=[
            StatusCodeInAssertion([200, 401]),
            LatencyThresholdAssertion(800),
        ],
        extractors=[
            Extractor("auth_token", "data.token", "json_path"),
            Extractor("user_id", "data.userId", "json_path"),
        ],
        think_time=0.05,
    ))

    # 步骤2: 商品列表
    scenario.add_step(ScenarioStep(
        name="商品列表",
        request=HttpRequest(
            name="product_list",
            method=HttpMethod.GET,
            url="/api/products",
            query_params={"page": "${page}", "size": 20},
            headers={"Authorization": "Bearer ${auth_token}"},
        ),
        assertions=[
            SuccessAssertion(),
            JsonPathAssertion("total", validator=lambda v: isinstance(v, int) and v >= 0),
            LatencyThresholdAssertion(500),
        ],
        extractors=[
            Extractor("first_product_id", "items[0].id", "json_path"),
        ],
        think_time=0.05,
    ))

    # 步骤3: 商品详情
    scenario.add_step(ScenarioStep(
        name="商品详情",
        request=HttpRequest(
            name="product_detail",
            method=HttpMethod.GET,
            url="/api/products/${first_product_id}",
        ),
        assertions=[
            SuccessAssertion(),
            JsonPathAssertion("data.id", validator=lambda v: v is not None),
            LatencyThresholdAssertion(300),
        ],
        think_time=0.05,
    ))

    # 步骤4: 加购
    scenario.add_step(ScenarioStep(
        name="加入购物车",
        request=HttpRequest(
            name="add_cart",
            method=HttpMethod.POST,
            url="/api/cart/items",
            body={"productId": "${first_product_id}", "quantity": 1},
            content_type="application/json",
        ),
        assertions=[
            StatusCodeInAssertion([200, 409]),
            LatencyThresholdAssertion(500),
        ],
        think_time=0.05,
    ))

    # 步骤5: 下单
    scenario.add_step(ScenarioStep(
        name="提交订单",
        request=HttpRequest(
            name="checkout",
            method=HttpMethod.POST,
            url="/api/orders",
            body={"items": [{"productId": "${first_product_id}"}], "payment": "alipay"},
            content_type="application/json",
        ),
        assertions=[
            StatusCodeInAssertion([200, 409, 429, 500]),
        ],
        continue_on_failure=True,
    ))

    return scenario


def main():
    scenario = build_test_scenario()
    print(f"🎯 场景: {scenario.name}")
    print(f"   步骤: {len(scenario.steps)} | 参数: {len(scenario.parameters)}")
    print(f"   模式: 恒定负载 | 并发: 8 | 时长: 15s | QPS: 50")
    print()

    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=15,
        concurrency=8,
        qps=50,
        warmup=2,
        report_dir="./reports",
        report_name="integration_test",
        output_console=True,
        output_json=True,
        output_html=True,
    )

    engine = LoadTestEngine(
        config=config,
        custom_http_executor=mock_http_executor,
    )

    result = engine.run()
    print()
    print("=" * 60)
    m = result.metrics
    print(f"✅ 集成测试完成")
    print(f"   总请求数: {m.throughput.total_requests:,}")
    print(f"   成功率: {(1 - m.errors.error_rate) * 100:.2f}%")
    print(f"   平均QPS: {m.throughput.overall_qps:,.1f}")
    print(f"   P50/P95/P99延迟: {m.overall.p50_ms:.1f}ms / {m.overall.p95_ms:.1f}ms / {m.overall.p99_ms:.1f}ms")
    print(f"   报告文件: {list(result.report_paths.values())}")
    print("=" * 60)

    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
