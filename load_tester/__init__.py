"""负载测试工具包
提供场景定义、压力生成、指标采集、统计聚合和报告输出功能。

快速使用:
```python
from load_tester import (
    LoadTestEngine, LoadTestConfig,
    Scenario, ScenarioStep, HttpRequest, HttpMethod,
    SuccessAssertion, JsonPathAssertion, LatencyThresholdAssertion,
)

# 定义场景
scenario = Scenario(name="示例API测试", base_url="https://api.example.com")
scenario.add_step(ScenarioStep(
    name="获取用户列表",
    request=HttpRequest(method=HttpMethod.GET, url="/users", name="list_users"),
    assertions=[SuccessAssertion(), LatencyThresholdAssertion(500)],
))

# 运行压测
config = LoadTestConfig(
    scenario=scenario,
    load_mode="constant",
    duration=60,
    concurrency=20,
    qps=100,
)
result = LoadTestEngine(config).run()
```
"""

from .engine import LoadTestConfig, LoadTestEngine, LoadTestResult
from .scenario import (
    Assertion,
    BodyContainsAssertion,
    ConstantParameter,
    CounterParameter,
    CsvParameter,
    CustomAssertion,
    CustomParameter,
    DatetimeParameter,
    Extractor,
    HeaderExistsAssertion,
    HeaderValueAssertion,
    HttpRequest,
    HttpMethod,
    JsonPathAssertion,
    LatencyThresholdAssertion,
    Parameter,
    ParameterSet,
    ParameterType,
    RandomChoiceParameter,
    RandomFloatParameter,
    RandomIntParameter,
    RandomStringParameter,
    Request,
    RequestResult,
    ResponseData,
    Scenario,
    ScenarioContext,
    ScenarioResult,
    ScenarioStep,
    ScenarioStepResult,
    SequenceParameter,
    StatusCodeAssertion,
    StatusCodeInAssertion,
    SuccessAssertion,
    TimestampParameter,
    UuidParameter,
)
from .generator import (
    ConstantLoadModel,
    LoadModel,
    LeakyBucketRateLimiter,
    RampUpLoadModel,
    RateLimiter,
    SpikeLoadModel,
    StepLoadModel,
    TokenBucketRateLimiter,
    Worker,
    WorkerPool,
    WorkerResult,
)
from .metrics import (
    MetricsCollector,
    MetricsSink,
    RealTimeSink,
    RecordingSink,
    Sample,
    SampleStatus,
    SampleType,
    WindowedSink,
)
from .report import ConsoleReporter, HtmlReporter, JsonReporter
from .stats import (
    AggregatedMetrics,
    ErrorMetrics,
    HdrHistogram,
    HistogramSnapshot,
    MetricsAggregator,
    PercentileMetrics,
    ThroughputMetrics,
)

__version__ = "1.0.0"

__all__ = [
    # Engine
    "LoadTestConfig",
    "LoadTestEngine",
    "LoadTestResult",
    # Scenario
    "Request",
    "HttpRequest",
    "HttpMethod",
    "RequestResult",
    "ResponseData",
    "Assertion",
    "StatusCodeAssertion",
    "StatusCodeInAssertion",
    "SuccessAssertion",
    "BodyContainsAssertion",
    "BodyMatchesAssertion",
    "JsonPathAssertion",
    "HeaderExistsAssertion",
    "HeaderValueAssertion",
    "LatencyThresholdAssertion",
    "CustomAssertion",
    "Parameter",
    "ParameterSet",
    "ParameterType",
    "ConstantParameter",
    "RandomIntParameter",
    "RandomFloatParameter",
    "RandomStringParameter",
    "RandomChoiceParameter",
    "SequenceParameter",
    "UuidParameter",
    "CounterParameter",
    "CsvParameter",
    "DatetimeParameter",
    "TimestampParameter",
    "CustomParameter",
    "Scenario",
    "ScenarioStep",
    "ScenarioContext",
    "ScenarioResult",
    "ScenarioStepResult",
    "Extractor",
    # Generator
    "Worker",
    "WorkerPool",
    "WorkerResult",
    "RateLimiter",
    "TokenBucketRateLimiter",
    "LeakyBucketRateLimiter",
    "LoadModel",
    "ConstantLoadModel",
    "StepLoadModel",
    "RampUpLoadModel",
    "SpikeLoadModel",
    # Metrics
    "Sample",
    "SampleType",
    "SampleStatus",
    "MetricsCollector",
    "MetricsSink",
    "RealTimeSink",
    "RecordingSink",
    "WindowedSink",
    # Stats
    "HdrHistogram",
    "HistogramSnapshot",
    "MetricsAggregator",
    "AggregatedMetrics",
    "PercentileMetrics",
    "ThroughputMetrics",
    "ErrorMetrics",
    # Report
    "ConsoleReporter",
    "JsonReporter",
    "HtmlReporter",
]
