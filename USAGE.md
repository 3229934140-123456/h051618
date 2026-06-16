# 负载测试工具 - 使用说明

## 目录结构

```
load_tester/
├── __init__.py           # 顶层导出
├── __main__.py           # python -m load_tester 入口
├── engine.py             # 压测引擎（核心，串联所有模块）
├── cli.py                # 命令行接口
├── scenario/             # 场景定义模块
│   ├── request.py        # HttpRequest / 请求定义
│   ├── assertion.py      # 多种断言
│   ├── parameter.py      # 参数化策略
│   └── scenario.py       # 场景编排 / 步骤 / 提取器
├── generator/            # 压力生成模块
│   ├── rate_limiter.py   # 令牌桶速率限制
│   ├── worker.py         # Worker / WorkerPool（并发用户）
│   └── load_model.py     # 4种负载模型
├── metrics/              # 指标采集模块
│   ├── sample.py         # Sample数据结构
│   └── collector.py      # 采集器 / 多种Sink
├── stats/                # 统计聚合模块
│   ├── histogram.py      # HDR直方图（分位延迟）
│   └── aggregator.py     # 实时聚合器
└── report/               # 报告输出模块
    ├── console.py        # 控制台ASCII报告
    ├── json_report.py    # JSON结构化报告
    └── html_report.py    # HTML可视化报告（SVG图表）
examples/
├── ecommerce_scenario.py # 完整电商用户流程示例
└── simple_health.py      # 最简健康检查示例
```

## 安装依赖

```bash
pip install requests  # 必需 - HTTP客户端
```

## 两种使用方式

### 方式1：Python API（推荐灵活使用）

```python
from load_tester import (
    LoadTestEngine, LoadTestConfig,
    Scenario, ScenarioStep, HttpRequest, HttpMethod,
    SuccessAssertion, JsonPathAssertion, LatencyThresholdAssertion,
    RandomIntParameter, Extractor,
)

# 1. 定义场景
scenario = Scenario(name="API测试", base_url="https://api.example.com")

# 注册参数（每个用户独立）
scenario.add_parameter(RandomIntParameter("item_id", 1, 10000))

# 添加步骤
scenario.add_step(ScenarioStep(
    name="获取详情",
    request=HttpRequest(
        method=HttpMethod.GET,
        url="/api/items/${item_id}",
        headers={"Authorization": "Bearer xxx"},
        timeout=5.0,
    ),
    assertions=[
        SuccessAssertion(),
        LatencyThresholdAssertion(500),  # < 500ms
        JsonPathAssertion("data.id", validator=lambda v: v is not None),
    ],
    extractors=[Extractor(name="fetched_id", target="data.id", source="json_path")],
    think_time=0.3,
))

# 2. 配置压测
config = LoadTestConfig(
    scenario=scenario,
    load_mode="constant",    # constant / step / ramp / spike
    duration=120,            # 秒
    concurrency=50,          # 并发用户数
    qps=500,                 # 目标QPS（None=不限）
    warmup=5,                # 预热时间
    report_dir="./reports",
    report_name="my_test",
    output_html=True,
    output_json=True,
)

# 3. 运行
result = LoadTestEngine(config).run()

# 4. 使用结果
print(f"总请求: {result.metrics.throughput.total_requests}")
print(f"P99延迟: {result.metrics.overall.p99_ms:.2f}ms")
print(f"错误率: {result.metrics.errors.error_rate * 100:.4f}%")
```

### 方式2：命令行 CLI

```bash
# 查看场景信息（不执行）
python -m load_tester.cli list examples/ecommerce_scenario.py

# 或者
python -m load_tester.cli run examples/ecommerce_scenario.py --list-only

# 恒定负载：20并发，60秒，目标200QPS
python -m load_tester.cli run examples/ecommerce_scenario.py \
    --mode constant \
    -c 20 -d 60 -q 200

# 阶梯加压：3个阶梯
python -m load_tester.cli run examples/ecommerce_scenario.py \
    --mode step \
    --steps "30,10,100;60,30,300;60,50,500"

# 平滑渐增：从10用户到200用户，180秒
python -m load_tester.cli run examples/ecommerce_scenario.py \
    --mode ramp \
    --start-concurrency 10 \
    --end-concurrency 200 \
    --ramp-duration 180 \
    --hold-end 60

# 尖峰测试：基线(10并发, 30s) → 尖峰(300并发, 20s) → 回落
python -m load_tester.cli run examples/ecommerce_scenario.py \
    --mode spike \
    --base-concurrency 10 --base-duration 30 \
    --spike-concurrency 300 --spike-duration 20 \
    --spike-count 2
```

## 核心概念速查

### 场景定义 (Scenario)

| 组件 | 说明 |
|------|------|
| `Scenario` | 完整业务流程，包含多个步骤 |
| `ScenarioStep` | 单个步骤 = 请求 + 断言 + 提取器 |
| `HttpRequest` | HTTP请求定义，支持 `${var}` 模板 |
| `Extractor` | 从响应提取数据到上下文，实现步骤间传值 |

### 请求模板语法

URL、Headers、Body 都支持 `${变量名}` 占位符，变量来源：
1. **参数化**：`scenario.add_parameter(XXXParameter("name", ...))`
2. **提取器**：前序步骤的 `Extractor(name="X")` 把值写入上下文
3. **内联参数**：query_params/body 中直接放 Parameter 实例

### 参数类型 (Parameter)

| 类型 | 说明 | 示例场景 |
|------|------|---------|
| `ConstantParameter` | 固定值 | 环境名、App版本 |
| `RandomIntParameter` | 随机整数 | 随机ID、分页页码 |
| `RandomFloatParameter` | 随机浮点数 | 经纬度、价格 |
| `RandomStringParameter` | 随机字符串 | 用户名、随机Token |
| `RandomChoiceParameter` | 列表选择（支持权重） | 地区、支付方式 |
| `SequenceParameter` | 顺序遍历 | 测试数据 |
| `UuidParameter` | UUID | 幂等键、追踪ID |
| `CounterParameter` | 递增计数器 | 序号、用户编号 |
| `CsvParameter` | CSV数据源 | 测试数据集 |
| `DatetimeParameter` | 日期时间字符串 | 时间戳字段 |
| `TimestampParameter` | 时间戳 | Unix时间戳 |
| `CustomParameter` | 自定义生成函数 | 任意逻辑 |

### 断言类型 (Assertion)

| 类型 | 检查内容 |
|------|---------|
| `StatusCodeAssertion(200)` | 状态码等于 |
| `StatusCodeInAssertion([200, 201])` | 状态码在列表中 |
| `SuccessAssertion()` | 2xx（推荐默认） |
| `BodyContainsAssertion("关键字")` | 响应体包含 |
| `BodyMatchesAssertion("regex")` | 响应体正则匹配 |
| `JsonPathAssertion("data.items[0].id")` | JSON路径值检查 |
| `HeaderExistsAssertion("X-Trace-Id")` | 响应头存在 |
| `HeaderValueAssertion("X-RateLimit", "1000")` | 响应头值 |
| `LatencyThresholdAssertion(500)` | 延迟阈值(ms) |
| `CustomAssertion(func)` | 自定义函数 |

### 负载模型 (Load Mode)

| 模式 | 适合场景 | 特点 |
|------|---------|------|
| `constant` | 稳定性测试、基线测试 | 固定并发+QPS，长时间运行 |
| `step` | 找性能拐点 | 阶梯式加压，观察每级表现 |
| `ramp` | 容量规划、平滑增长 | 线性增加，无突变 |
| `spike` | 秒杀、突发流量 | 基线→尖峰→回落 |

### 报告输出

运行后会生成报告到 `./reports/`：
- `xxx.html` - **推荐**：交互式图表，适合阅读
- `xxx.json` - 机器可读，适合CI集成和二次分析
- 控制台输出 - 快速查看核心指标

## CI 集成示例

```bash
# 运行压测并通过返回码判断
python -m load_tester.cli run examples/simple_health.py \
    -c 10 -d 30 -q 100 \
    --report-dir ./ci-reports

EXIT_CODE=$?
if [ $EXIT_CODE -eq 0 ]; then
    echo "✅ 压测通过（错误率<1%）"
elif [ $EXIT_CODE -eq 1 ]; then
    echo "❌ 压测失败（错误率>5%）"
    exit 1
elif [ $EXIT_CODE -eq 2 ]; then
    echo "⚠️  压测被中断"
    exit 2
fi
```
