"""最简单的示例场景：单请求 + 参数化

演示最基础的使用方式。
"""
from load_tester import (
    Scenario,
    ScenarioStep,
    HttpRequest,
    HttpMethod,
    SuccessAssertion,
    StatusCodeInAssertion,
    LatencyThresholdAssertion,
    RandomIntParameter,
    RandomStringParameter,
)


def build_scenario() -> Scenario:
    scenario = Scenario(
        name="简单API健康检查",
        description="对多个端点进行健康检查压测",
        base_url="http://localhost:3000",
    )

    # 随机ID参数
    scenario.add_parameter(RandomIntParameter(
        name="user_id",
        min_value=1,
        max_value=10000,
    ))
    scenario.add_parameter(RandomStringParameter(
        name="session_id",
        min_length=32,
        max_length=32,
        charset="abcdef0123456789",
    ))

    # 首页健康检查
    scenario.add_step(ScenarioStep(
        name="health_check",
        request=HttpRequest(
            name="health",
            method=HttpMethod.GET,
            url="/health",
            timeout=2.0,
        ),
        assertions=[
            StatusCodeInAssertion([200, 204], name="健康端点状态码200/204"),
            LatencyThresholdAssertion(100),  # < 100ms
        ],
    ))

    return scenario


if __name__ == "__main__":
    from load_tester import LoadTestEngine, LoadTestConfig

    config = LoadTestConfig(
        scenario=build_scenario(),
        load_mode="constant",
        duration=10,
        concurrency=3,
        warmup=1,
        report_dir="./reports",
        report_name="simple_health",
    )
    LoadTestEngine(config).run()
