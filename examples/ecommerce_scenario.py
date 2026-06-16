"""示例压测场景 - RESTful API 典型用户登录流程

演示：
1. 多步骤请求序列（登录 → 获取列表 → 查看详情 → 提交操作）
2. 参数化（随机用户名、计数器、随机选择）
3. 数据提取器（从登录响应提取token，后续步骤使用）
4. 多种断言（状态码、JSON路径、响应体包含、延迟阈值）
5. 思考时间（模拟真实用户操作间隔）
"""
from load_tester import (
    Scenario,
    ScenarioStep,
    HttpRequest,
    HttpMethod,
    SuccessAssertion,
    StatusCodeAssertion,
    StatusCodeInAssertion,
    JsonPathAssertion,
    BodyContainsAssertion,
    LatencyThresholdAssertion,
    HeaderExistsAssertion,
    Extractor,
    ConstantParameter,
    RandomIntParameter,
    RandomStringParameter,
    RandomChoiceParameter,
    CounterParameter,
    UuidParameter,
)


def build_scenario() -> Scenario:
    """构建一个模拟典型电商用户流程的压测场景"""

    # 1. 创建场景（Base URL 可以在运行时配置）
    scenario = Scenario(
        name="电商用户典型流程",
        description="模拟用户：首页 → 登录 → 浏览商品 → 查看详情 → 加购 → 下单",
        base_url="${PROTOCOL:-http://localhost:8080}",
        default_headers={
            "User-Agent": "LoadTester/1.0",
            "Accept": "application/json",
        },
        iteration_pause=1.0,
    )

    # 2. 注册参数（每个虚拟用户独立的参数空间）
    scenario.add_parameter(RandomStringParameter(
        name="username",
        min_length=6,
        max_length=12,
        charset="abcdefghijklmnopqrstuvwxyz0123456789",
        prefix="user_",
    ))
    scenario.add_parameter(CounterParameter(
        name="user_seq",
        start=10000,
        step=1,
    ))
    scenario.add_parameter(RandomStringParameter(
        name="password",
        min_length=10,
        max_length=16,
    ))
    scenario.add_parameter(RandomChoiceParameter(
        name="user_region",
        choices=["CN", "US", "EU", "JP", "SG"],
        weights=[0.5, 0.2, 0.15, 0.1, 0.05],
    ))
    scenario.add_parameter(RandomIntParameter(
        name="product_id_min",
        min_value=1000,
        max_value=9000,
    ))
    scenario.add_parameter(UuidParameter(
        name="trace_id",
        uuid_version=4,
    ))

    # ============== 步骤 1: 访问首页（匿名）==============
    scenario.add_step(ScenarioStep(
        name="步骤1_访问首页",
        request=HttpRequest(
            name="homepage",
            method=HttpMethod.GET,
            url="/api/home",
            headers={
                "X-Trace-Id": "${trace_id}",
                "X-Region": "${user_region}",
            },
            timeout=5.0,
        ),
        assertions=[
            SuccessAssertion(),
            LatencyThresholdAssertion(300),  # 首页响应时间 < 300ms
            HeaderExistsAssertion("Content-Type"),
            JsonPathAssertion(
                json_path="banner",
                validator=lambda v: isinstance(v, list) and len(v) > 0,
                name="banner列表非空",
            ),
        ],
        think_time=0.5,  # 模拟用户看首页0.5秒
    ))

    # ============== 步骤 2: 登录 ==============
    scenario.add_step(ScenarioStep(
        name="步骤2_用户登录",
        request=HttpRequest(
            name="login",
            method=HttpMethod.POST,
            url="/api/auth/login",
            headers={
                "X-Trace-Id": "${trace_id}",
            },
            body={
                "username": "${username}",
                "password": "${password}",
                "region": "${user_region}",
            },
            content_type="application/json",
            timeout=10.0,
        ),
        assertions=[
            StatusCodeAssertion(200, name="登录状态码200"),
            JsonPathAssertion(json_path="code", expected_value=0, name="业务码为0"),
            JsonPathAssertion(
                json_path="data.token",
                validator=lambda v: isinstance(v, str) and len(v) > 32,
                name="token非空且长度足够",
            ),
            JsonPathAssertion(
                json_path="data.userId",
                validator=lambda v: v is not None,
                name="userId存在",
            ),
            LatencyThresholdAssertion(1000),  # 登录 < 1秒
        ],
        extractors=[
            Extractor(
                name="auth_token",
                target="data.token",
                source="json_path",
            ),
            Extractor(
                name="user_id",
                target="data.userId",
                source="json_path",
            ),
        ],
        think_time=1.0,
    ))

    # ============== 步骤 3: 获取商品列表（需要登录token） ==============
    scenario.add_step(ScenarioStep(
        name="步骤3_浏览商品列表",
        request=HttpRequest(
            name="product_list",
            method=HttpMethod.GET,
            url="/api/products",
            headers={
                "Authorization": "Bearer ${auth_token}",
                "X-Trace-Id": "${trace_id}",
                "X-User-Id": "${user_id}",
            },
            query_params={
                "page": "${product_id_min}",  # 重用参数
                "size": 20,
                "category": RandomChoiceParameter(
                    name="category",
                    choices=["electronics", "clothing", "food", "books"],
                ),  # 内联参数定义
                "sort": "price_asc",
            },
            timeout=5.0,
        ),
        assertions=[
            SuccessAssertion(),
            JsonPathAssertion(
                json_path="items",
                validator=lambda v: isinstance(v, list),
                name="items是数组",
            ),
            JsonPathAssertion(
                json_path="total",
                validator=lambda v: isinstance(v, int) and v >= 0,
                name="total为非负整数",
            ),
            LatencyThresholdAssertion(500),
        ],
        extractors=[
            Extractor(
                name="first_product_id",
                target="items[0].id",
                source="json_path",
            ),
        ],
        think_time=2.0,  # 模拟用户浏览2秒
    ))

    # ============== 步骤 4: 查看商品详情 ==============
    scenario.add_step(ScenarioStep(
        name="步骤4_查看商品详情",
        request=HttpRequest(
            name="product_detail",
            method=HttpMethod.GET,
            url="/api/products/${first_product_id}",
            headers={
                "Authorization": "Bearer ${auth_token}",
                "X-Trace-Id": "${trace_id}",
            },
            timeout=3.0,
        ),
        assertions=[
            StatusCodeAssertion(200),
            JsonPathAssertion(
                json_path="id",
                validator=lambda v: v is not None,
                name="商品ID存在",
            ),
            JsonPathAssertion(
                json_path="price",
                validator=lambda v: isinstance(v, (int, float)) and v > 0,
                name="价格为正数",
            ),
            BodyContainsAssertion("name", case_sensitive=False),
            LatencyThresholdAssertion(300),
        ],
        think_time=3.0,  # 模拟用户查看详情3秒
    ))

    # ============== 步骤 5: 加入购物车 ==============
    scenario.add_step(ScenarioStep(
        name="步骤5_加入购物车",
        request=HttpRequest(
            name="add_to_cart",
            method=HttpMethod.POST,
            url="/api/cart/items",
            headers={
                "Authorization": "Bearer ${auth_token}",
                "X-Trace-Id": "${trace_id}",
                "X-User-Id": "${user_id}",
            },
            body={
                "productId": "${first_product_id}",
                "quantity": RandomIntParameter(
                    name="qty",
                    min_value=1,
                    max_value=5,
                ),
            },
            content_type="application/json",
            timeout=3.0,
        ),
        assertions=[
            StatusCodeInAssertion([200, 201], name="加购状态码200/201"),
            JsonPathAssertion(json_path="success", expected_value=True),
            LatencyThresholdAssertion(500),
        ],
        think_time=1.0,
    ))

    # ============== 步骤 6: 提交订单（高价值操作，允许失败不中断） ==============
    scenario.add_step(ScenarioStep(
        name="步骤6_提交订单",
        request=HttpRequest(
            name="checkout",
            method=HttpMethod.POST,
            url="/api/orders",
            headers={
                "Authorization": "Bearer ${auth_token}",
                "X-Trace-Id": "${trace_id}",
                "X-User-Id": "${user_id}",
                "Idempotency-Key": "${trace_id}-${user_seq}",
            },
            body={
                "items": [
                    {"productId": "${first_product_id}", "quantity": 1}
                ],
                "addressId": RandomIntParameter(
                    name="addr_id",
                    min_value=1,
                    max_value=100,
                ),
                "paymentMethod": RandomChoiceParameter(
                    name="pay_method",
                    choices=["alipay", "wechat", "card"],
                    weights=[0.5, 0.4, 0.1],
                ),
            },
            content_type="application/json",
            timeout=15.0,
        ),
        assertions=[
            StatusCodeInAssertion([200, 201, 409, 429], name="允许409冲突/429限流"),
            # 注意：409/429也是成功请求的合法响应，业务断言只在200/201时检查
        ],
        extractors=[
            Extractor(
                name="last_order_id",
                target="data.orderId",
                source="json_path",
                default=None,
            ),
        ],
        think_time=0,
        continue_on_failure=True,  # 下单失败也不影响整体场景
        weight=3,  # 权重标记（仅用于统计，不影响执行频率）
    ))

    return scenario


# 如果直接运行此脚本，也可以直接执行
if __name__ == "__main__":
    import sys
    from load_tester import LoadTestEngine, LoadTestConfig

    scenario = build_scenario()

    # 检查是否有 --dry-run 参数
    if "--dry-run" in sys.argv or "-n" in sys.argv:
        # 仅打印场景信息
        print(f"场景名称: {scenario.name}")
        print(f"步骤数量: {len(scenario.steps)}")
        print(f"参数数量: {len(scenario.parameters)}")
        for i, step in enumerate(scenario.steps, 1):
            print(f"\n  步骤 {i}: {step.name}")
            print(f"    {step.request.method.value} {step.request.url}")
            print(f"    断言数量: {len(step.assertions)}")
            if step.extractors:
                print(f"    提取器: {[e.name for e in step.extractors]}")
    else:
        # 简单运行一下（对本地服务，目标不存在会报错，但结构正确）
        config = LoadTestConfig(
            scenario=scenario,
            load_mode="constant",
            duration=30,
            concurrency=5,
            qps=10,
            warmup=2,
            report_dir="./reports",
            report_name="ecommerce_demo",
        )
        result = LoadTestEngine(config).run()
