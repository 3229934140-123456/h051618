# 负载测试工具 — 技术原理说明

> 详细说明各模块的设计思路，以及核心技术选型原因。

---

## 一、场景定义模块 — 请求序列与参数化

### 1.1 请求序列：模板变量替换

**文件**：[request.py (load_tester/scenario/request.py)

**设计：** 每个 `HttpRequest` 中的 `resolve_* 系列方法实现模板替换。

原理：

```
用户写：
URL模板（场景参数集 → 场景上下文 → resolve_url/resolve_headers/resolve_body
                                                    ↓
                                       正则 \$\{变量名\} → 字典查找 → 字符串替换
```

**关键机制**：

| 层次 | 说明 |
|--------|------|
| URL模板 | `https://api.example.com/users/${userId}?page=${page} |
| Header模板 | `Authorization: Bearer ${token}` |
| Body模板 | `{"id": "${itemId}", "qty": ${qty}}` |
| Query模板 | `params  `itemId` 从 `_build` 循环` - |

### 1.2 参数化：参数空间独立

**文件**：[parameter.py](load_tester/scenario/parameter.py)

**核心设计：** 每个虚拟用户（Worker）拥有独立的 `ScenarioContext`，参数实例——参数实例—— `ParameterSet.generate()` 生成：

```
Parameter types:
├── ConstantParameter     # 常数
├── Random*Parameter       # 随机族（独立Random实例）
│   ├── RandomInt/Float/String/Choice
│   └── 每个实例有自己的Random实例（seed可选，保证可重复）
├── SequenceParameter     # 顺序（per_worker：每个Worker独立序列
├── CounterParameter      # 计数器
├── UuidParameter         # UUID
├── CsvParameter         # CSV读取
└── CustomParameter       # 任意函数
```

**参数依赖顺序**：生成时按照添加顺序生成，**后续参数可以引用前面参数变量（context）：

```python
# 上下文context["A"] = A 生成 A → B生成时 context 可用
scenario.add_parameter(A)
scenario.add_parameter(B依赖 A) → 生成 B 生成
```

### 1.3 断言与提取器：数据流

**文件**：[assertion.py](load_tester/scenario/assertion.py)、[scenario.py](load_tester/scenario/scenario.py)

```
请求 → → 执行流：
                    ┌─────────────────────────────────────┐
                    │  ScenarioStep.execute()          │
                    │                            │
  HttpRequest → resolve → │  1. 请求                    │
                    │  2. HTTP执行（外部调用）│
                    │  ↓ ResponseData             │
                    │  3. 断言列表 assert_all() │ → AssertionResult[]
                    │  ↓                         │
                    │  4. 提取器列表提取        │ → 更新 ScenarioContext.variables
                    │  → ScenarioContext          │
                    └─────────────────────────────────────┘
```

**断言类型矩阵**：

| 断言类 | 检查时机 | 耗时 |
|----------|------------|--------|
| StatusCode* | O(1) | 超快 |
| BodyContains | O(n) 字符串包含 | O(n) |
| JsonPath | JSON解析 O(n) | 解析+路径 |
| LatencyThreshold | O(1) 响应已记录 | 微秒 |
| CustomAssertion | 任意函数 | 任意 |

**关键：** JSON Path 实现简单JMESPath语法：`items[0].name`、`users[].id` 等简化实现。

---

## 二、压力生成 — 工作池并发模型

### 2.1 Worker = 虚拟用户模型

**文件**：[worker.py](load_tester/generator/worker.py)

**核心原则**：**1 Worker = 1 线程 = 1 独立会话 = 1 ScenarioContext
```
┌─────────────────────────────────────────────────────────────┐
│                     WorkerPool 线程:4 ──────────── → 调度
│                                                           │
│  Worker-0 (Thread-0)       Worker-1 (Thread-1)  ... │
│  ┌─────────────────────┐  ┌─────────────────────┐     │
│  │ ScenarioContext  │  │ ScenarioContext     │     │
│  │ - variables     │  │ - variables    │     │
│  │ - cookies     │  │ - cookies    │     │
│  │ - headers  │  │ - headers      │     │
│  └────────┬────────┘  └────────┬────────┘     │
│           │                      │                  │
│           │  HTTP执行  │                  │
│           ▼                     ▼                  │
│       requests库调用   │
│           │                      │                  │
│           ▼                      ▼                  │
│       Scenario.run_iteration()      ...    │
│           │                      │                  │
│           └──────────┬───────────┘                  │
│                      │ 结果                     │
│                      ▼                          │
│           result_callback(WorkerResult) → MetricsCollector
└──────────────────────────────────────────────────────────┘
```

**为何用线程不用协程？**

| 维度 | 线程 (当前选择) | 协程 (asyncio) |
|--------|----------------|------------------|
| HTTP客户端 | requests（成熟、稳定 | aiohttp（需事件循环） |
| GIL影响 | IO密集无影响（socket） | 单线程内并发 |
| 调试便利 | 栈traceback清晰 | 协程切换难调试困难 |
| 阻塞风险 | 一个线程阻塞不影响 | 任何调用任何阻塞阻塞 |
| 真实用户型 | 最接近真实 | 比实际 |

### 2.2 工作生命周期：
    ┌启动 (start() → _run() 主循环：
  ```while not _should_stop():
      ① rate_limiter 每次）→ WorkerResult)
        └ → 下一次循环（迭代pause

**Worker异常自动重启**：Monitor线程每500ms检查Worker异常 → 自动重启死掉的Worker → 保持目标并发数动态伸缩：

```
WorkerPool.scale_up(n)    # scale_to(target)
          │
        ├── 直接启动线程 增加/减少 worker
          └── scale_down(n)   # 优雅停止 remove +  join 让当前
```

## 三、速率精确控制：令牌桶

### 3.1 令牌桶算法

**文件**：[rate_limiter.py](load_tester/generator/rate_limiter.py)

**原理**：

```
时间 → 桶容量 = 1 令牌/秒 = QPS
   │
   │ 补充速率 = QPS 令牌/秒
   │
   ▼
桶（Bucket:
  ＿＿＿＿＿＿＿＿＿＿＿＿＿＿
  │  当前tokens = min(max_burst, tokens + elapsed × ＿elapsed × rate)
  │  ↓
  │  acquire(n)：
  │    tokens ≥ n → tokens -= n → 立即返回
  │    tokens < n → 需要等 (n - tokens) / rate 秒
  └────────────────────────────────

**关键参数对比：

| 元素 | 值 | 说明 |
|--------|-----|------|
| `max_burst` | `rate_per_second | 默认允许突发1秒的token |
| `busy_wait` | True | 短等待用 CPU 循环不释放精度 |
| `busy_wait_threshold` | 5ms | ≤5ms忙等，>5ms sleep+busy尾 |

### 3.2 为什么 sleep 的等待策略

```
等待 Δt = 需要等待的精确控制了两种：
├─短等待 (<= 5ms → 忙等 while time.perf_counter 自旋
│   优点：微秒级精度（不调度
│   缺点：占用CPU核心）
│
长等待 (> 5ms → sleep → 策略：
│   ① time.sleep(Δt - 5ms)   （释放CPU）
│   ② 剩余5ms → 忙等收尾   （修正 sleep 粒度粗误差）
```

### 3.3 阶梯加压实现：

**文件**：[load_model.py](load_tester/generator/load_model.py)

```

恒定负载 (ConstantLoadModel)
```
并发数/秒 (warmup → steady
   ←───────────
        concurrency/sec
```

**阶梯加压 (StepLoadModel)
```
并发
   concurrency3      step_3: C3
        │     step_2: C2
        │  step_1: C1  │
        │C0        │
        │  │        │  │
    warmup │  │        │  │
        └──┴──┴──┴──┴── time
           t0 t1 t2 t3
```

**每次 step_i 调整 set_rate() + WorkerPool.scale_to(Ci)

```

**平滑 RampUpLoadModel)
```
并发线性 interpolation
    end  interpolate
  │   每 adjust_interval (1s一次
  │    target_concurrency = start + (end-start) × progress
  │    WorkerPool.scale_to(→)
  │
start│  │  │  │  │  │  │  │
  └──┴──┴──┴──┴──┴──┴──┴── time
         每 step 秒：t
```

**Spike尖峰 (SpikeLoadModel)
基线 → ramp_up → spike → ramp_down
```

## 四、延迟：指标采集：每个请求的结果采集

### 4.1 高精度延迟计算

**文件**：[sample.py](load_tester/metrics/sample.py)、[collector.py](load_tester/metrics/collector.py)

**时钟选择** **采集中，**3 个时刻点：

```python
# 场景迭代内：
t0 = time.perf_counter# 开始（高精度纳秒级
requests.request()
t1 = time.perf_counter()
          │
latency = t1 - t0     → → response
```

**为什么用 `perf_counter` 不是 `time.time()`：

| 函数 | 精度 | 单调 | 受系统时间 | 用途
|------|------|--------|------------|--------|
| `time()` | ~1ms | ❌ | 是（NTP调整会跳）| 绝对时间戳
| `perf_counter` | ~ns | ✅ | 否 | 测量持续时间 | ✅ 本项目选择 | **用 perf_counter 计算 latency，用 time() 存 timestamp
```

### 4.2 采集架构：**流水线架构**

```
                    ┌───────────────────────────────────────────────┐
 Worker-0  │ Worker-1  ...     Worker-N
    │             │                  │
    │ record(Sample)     record(Sample)
    │             │                  │
    └────────┬─────────┴─────┘
              │
              ▼
    ┌──────────────────────┐
    │  queue.Queue  │← │  (有界 100K)
    │  thread-safe)  │
    └──────────┬──────────┘
              │  后台批量取出（1000条/10ms）
              ▼
    ┌─────────────────────────────────────────┐
    │ MetricsCollector._processing_loop   │
    │  批量 batch_size=1000
    │  dispatch_batch()
    └───────┬─────────────────────┘
              │
     ┌────────┼──────────┐
     ▼        ▼          ▼
Aggregator  WindowedSink  自定义
 (实时聚合) (滑窗监控)  (Prometheus等)
```

### 4.3 背压控制策略：

```
队列满（队列：
├─ Queue.Full → drop_on_full = True
│    └ 弹出最旧 → 放新
│    └ 警告降采样策略（不阻塞Worker不阻塞Worker
│
drop_on_full = False
     阻塞 queue.put 直到有空间
     └  压力上实际
```

## 五、统计聚合：直方图估计分位延迟

### 5.1 不存全量数据的 HDR **核心思想

**文件**：[histogram.py](load_tester/stats/histogram.py)

**问题**：1亿个延迟存全量 → 800MB内存 → 不可行

**HDR 分桶策略**：值 → **按指数增长精度：

```
值域（纳秒):

  低延迟区        中延迟区        高延迟区
  ┌───────┐   ┌──────────┐   ┌─────────────────┐
  │桶1:   │   │桶k:  │   │桶m:  │
  │1μs-1ms│   │1ms-1s   │   │1s-60s     │
  │子桶2048个│   │子桶2048个│   │子桶2048个│
  │每个子桶宽 │   │每个子桶宽  │   │每个子桶宽    │
  │0.5ns  │   │0.5μs  │   │0.5ms         │
  └───────┘   └──────────┘   └─────────────────┘
   ↑精度高         ↑中精度        ↑精度降
```

**数学原理**：

子桶数 = 2^ceil(log2(10^sig_digits))

| sig_digits = 3 → sub_bucket_count = 2048 → 误差 ≤ 0.1%

每个子桶的精度 = 值所在桶的桶宽度 / 子桶数

### 5.2 分位数计算（累加计数法：

```python
def get_value_at_percentile(self, percentile):
    target_count = ceil(total_count × percentile / 100)
    accumulated = 0

    for 每个桶:
        for 每个子桶:
            accumulated += count[桶][子桶]
            if accumulated >= target_count:
                return 子桶值
```

**分位数计算复杂度**：O(bucket_count × sub_bucket_count)
≈ O(50 × 1024) ≈ **5万次循环 ≈ 微秒级

### 5.3 内存占用分析：

| 配置 | 桶数 | 子桶数 | 总计数器数 | 内存 |
|--------|-------|----------|-----------|------|
| 1μs - 60s，3位有效 | ~50 | 2048 | ~100K | ~800KB |
| 1μs - 10min，3位 | ~60 | 2048 | ~120K | ~960KB |
| 1ns - 24h，4位 | ~75 | 16384 | ~1.2M | ~9.6MB |

对比存全量：**1亿样本 × 8字节 = **800MB → HDR直方图节省 ~1000× 内存

### 5.4 标准分位输出：

| 分位 | 典型用途 |
|--------|---------|
| p50 (中位数) | 典型用户体验 |
| p75 | 多数用户 |
| p90 | 长尾起始 |
| p95 | SLA 常用 |
| p99 | SLA 严格 |
| p99.9 | 严苛服务 |
| p99.99 | 金融/高频 |

---

## 六、吞吐与错误率：实时聚合

### 6.1 聚合器（aggregator.py](load_tester/stats/aggregator.py)

增量聚合：每批样本增量更新计数

```
每批 来后：
```
  ① 每个sample：
    ├ 全局直方图 record_value(latency_ns)
    ├ 名称直方图 按名称直方图record_value()
    ├ 计数 ++total_count / _failure_count
    ├ status_counts[status] ++
    ├ if error → _error_types[status] ++
    ├ 时间分桶：
    │   bucket_ts = floor(ts) 按秒分桶)
    │   if 新 bucket 入队 deque(maxlen=3600)
    │   else 当前桶 current_total, _current_bucket_total++, _success++
```

### 6.2 吞吐计算：

**总体 QPS = total_count / duration           # 全程平均

**峰值 QPS = max(每桶计数 / bucket_size)   # 逐秒峰值

**实时 QPS（窗口）：最近10秒 bucket 总 / 10s

### 6.3 错误统计：

```
错误分类：
├── 按 状态码 by_status_code → {200: 12345, 404: 78, 500: 45}
├── 按 错误类型 type → timeout: 12, assertion: 30, ...
└── Top N 错误信息 → 前10高频错误
```

---

## 七、压测自身避免瓶颈

### 7.1 设计准则：

| 问题 | 解决策略 | 实现位置 |
|------|---------|---------|
| **CPU密集瓶颈 | 1. 异步解耦 批量处理 | collector.py queue |
| **内存泄漏 | 2. 有界队列 + HDR直方图 | histogram.py |
| **时钟开销 | 3. 忙等 精度优先（ | rate_limiter.py |
| **HTTP客户端连接池 | 4. requests keep-alive（Connection） | worker.py 线程池复用 |
| **GIL竞争 | 5. I/O 密集释放（网络I/O 无影响 | Threading |
| **线程调度抖动 | 6. 全局时间粒度 ≥ 10ms级 + 忙等 | rate_limiter.py |
| **GC停顿 | 7. 避免创建对象复用 + 少字符串模板 | request.py |

### 7.2 关键实现细节：

**(1) 异步解耦**：Worker只做HTTP、不直接聚合：Worker** - record() → 入队立即返回，聚合在后台线程批量处理，**不阻塞 Worker 的** Worker* 网络IO 延迟

**(2) 内存：**
- 队列 maxsize 限 → worker
- 直方图O(1) 增量 → O(n) 全量
- 时间分桶 deque → 每小时自动**滑动

**(3) 连接池复用**：
requests.Session自动TCP复用减少TCP握手3次握手

```python
# 默认 requests库 在每个线程里自动
# 每个线程 自动复用TCP连接
# 避免反复建连
```

### 7.3 自身性能监控：

**Collector 自身统计：

```python
CollectorStats:
  total_samples       # 采样本
  dropped_samples  # 丢弃（队列满）
  queue_size        # 当前队列
  batches_processed  # 处理批数
  avg_batch_size    # 平均批大小
  processing_errors # Sink错误
```

**调参经验法则**：
- dropped_samples 持续 > 0 → 聚合器能力不足 → 增大队列 / 优化Sink
- queue_size 持续接近max_size → 下游压力大
- processing_errors > 0 → Sink 自身

---

## 八、区分：被测服务问题 vs 压测客户端问题

### 8.1 判断矩阵：

| 现象 | 被测服务问题 | 压测端问题 |
|------|------------|------------|
| 延迟整体突增 + 错误率同时上升 | CPU/连接错误同时上升 |
| 错误类型 | 状态码5xx 429 TooManyRequests | ConnectionError / Timeout 本地 socket |
| 延迟上升 + CPU 压测端 CPU 满 | ❌ 不太可能 | ✅ 可能（线程 |
| 队列有 dropped_samples > 0 | ❌ 无关 | ✅ 采集能力不足 |
| 不同 Worker 数但QPS 线性增长到一定值后不再涨 | 服务瓶颈 | 客户端瓶颈 |
| 延迟分布 p99 高 延迟高 | 服务端处理变慢 | 网络 / 时钟调度延迟 |
| 单机多机分布 分布一致 | 服务端问题 | 单机差异 |

### 8.2 诊断步骤：

**：

```
步骤 基线：
  ├─ 1. 看错误分类：
  │   ├─ 5xx / 429 → 服务过载 / 限流
  │   ├─ ConnectionError / Timeout → 网络或建连
  │   ├─ Connection refused → 服务端口连接队列满
  │   └─ 断言失败 Assertion → 业务逻辑
  │
  2. 看延迟模式：
  │   ├─ p50稳定 p99 飙升 → 服务端有慢请求
  │   └─ 所有分位整体平移 → 网络延迟 / 客户端
  │
  3. 看自身指标：
  │   ├─ 队列 满 → 检查 Collector 下游慢
  │   ├─ 压测端 CPU 100% → 客户端
  │   └─ Worker 重启频繁 → Worker 异常崩溃 → 增加资源不够
  │
  4. 对照实验：
  │   ├─ 同一服务端小并发 → 是否可线性 → 客户端瓶颈
  │   ├─ 多压测机器分布式 → 效果 → 单实例瓶颈
  │   └─ 本地 ping 延迟 → 纯网络问题
  │
  └─ 5. 抓包分析：
      ├─ tcpdump / Wireshark → 查看 RTT
      └─ 服务端日志配合 | 看到连接建立) | 连接数

### 8.3 健康自检：

**客户端瓶颈 → 2-1 诊断客户端指标：

```bash
# CPU 监控：
# - htop → CPU
# - iostat → 磁盘IO
# - ss -s → TCP 状态
# - netstat → 端口
```

```python
# 代码内自检：
collector.get_stats() → 看 dropped, queue_size
worker_pool.active_workers → 看实际运行中 Worker数
aggregator.get_realtime_stats() → 看 qps 延迟
```

---

## 九、扩展方向：

```
本项目架构可扩展：

分布式：
├─ 分布式多机协调（主从
├── 协议支持（gRPC/ WebSocket 支持
├── 接入 Prometheus / Grafana）实时监控
├── InfluxDB +
└── 混沌 失败重试
├── 自动 录制 → 生成 → 回放
```
