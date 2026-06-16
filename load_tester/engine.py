"""压测引擎

串联所有模块，提供完整的压测执行流程：
1. 配置场景和负载模型
2. 初始化采集器、聚合器、报告器
3. 启动工作池执行压测
4. 实时报告进度
5. 压测结束后输出最终报告

这是使用本工具的主要入口类。
"""
from __future__ import annotations

import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type, Union

from .scenario import Scenario
from .generator import (
    ConstantLoadModel,
    LoadModel,
    RateLimiter,
    RampUpLoadModel,
    SpikeLoadModel,
    StepLoadModel,
    TokenBucketRateLimiter,
    WorkerPool,
)
from .metrics import MetricsCollector, Sample
from .report import ConsoleReporter, HtmlReporter, JsonReporter
from .stats import MetricsAggregator, AggregatedMetrics


@dataclass
class LoadTestConfig:
    """压测配置

    支持的负载模式：
    - constant: 恒定负载（默认）
    - step: 阶梯加压
    - ramp: 平滑渐增
    - spike: 尖峰突发
    """
    # 场景
    scenario: Scenario

    # 负载模式
    load_mode: str = "constant"

    # 恒定负载参数
    duration: float = 60.0
    concurrency: int = 10
    qps: Optional[float] = None
    warmup: float = 5.0

    # 阶梯负载参数
    steps: List = field(default_factory=list)  # [(duration, concurrency, qps), ...]

    # Ramp负载参数
    start_concurrency: int = 1
    end_concurrency: int = 100
    start_qps: Optional[float] = None
    end_qps: Optional[float] = None
    ramp_duration: float = 300.0
    hold_end_duration: float = 30.0

    # Spike负载参数
    base_duration: float = 60.0
    spike_duration: float = 30.0
    base_concurrency: int = 10
    spike_concurrency: int = 200
    base_qps: Optional[float] = 50.0
    spike_qps: Optional[float] = 1000.0
    spike_count: int = 1

    # 报告配置
    report_dir: Optional[str] = None
    report_name: str = "loadtest_report"
    output_console: bool = True
    output_json: bool = True
    output_html: bool = True

    # 高级配置
    rate_limiter_busy_wait: bool = True
    collector_queue_size: int = 100000
    histogram_sig_digits: int = 3
    enable_progress_bar: bool = True
    progress_interval: float = 0.2

    def build_load_model(self) -> LoadModel:
        """根据配置构建负载模型"""
        mode = self.load_mode.lower()

        if mode == "constant":
            return ConstantLoadModel(
                duration=self.duration,
                concurrency=self.concurrency,
                qps=self.qps,
                warmup=self.warmup,
            )
        elif mode == "step":
            if not self.steps:
                raise ValueError("Step mode requires 'steps' configuration")
            return StepLoadModel(
                steps=self.steps,
                warmup=self.warmup,
            )
        elif mode == "ramp":
            return RampUpLoadModel(
                duration=self.ramp_duration,
                start_concurrency=self.start_concurrency,
                end_concurrency=self.end_concurrency,
                start_qps=self.start_qps,
                end_qps=self.end_qps,
                warmup=self.warmup,
                hold_end_duration=self.hold_end_duration,
            )
        elif mode == "spike":
            return SpikeLoadModel(
                base_duration=self.base_duration,
                spike_duration=self.spike_duration,
                base_concurrency=self.base_concurrency,
                spike_concurrency=self.spike_concurrency,
                base_qps=self.base_qps,
                spike_qps=self.spike_qps,
                hold_count=self.spike_count,
                warmup=self.warmup,
            )
        else:
            raise ValueError(f"Unknown load mode: {mode}. Use constant/step/ramp/spike.")


@dataclass
class LoadTestResult:
    """压测最终结果"""
    config: LoadTestConfig
    metrics: AggregatedMetrics
    report_paths: Dict[str, str] = field(default_factory=dict)
    start_time: float = 0.0
    end_time: float = 0.0
    duration: float = 0.0
    stopped_early: bool = False
    # 参数使用统计（CSV、计数器等）
    parameter_stats: List[Dict[str, Any]] = field(default_factory=list)
    csv_stats: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return (
            self.metrics.errors.total_errors == 0
            or self.metrics.errors.error_rate < 0.01
        )


class StatsSinkAdapter:
    """将 MetricsAggregator 适配为 MetricsSink"""

    def __init__(self, aggregator: MetricsAggregator):
        self._aggregator = aggregator

    def write(self, samples: List[Sample]) -> None:
        self._aggregator.write(samples)


class LoadTestEngine:
    """压测引擎主类

    使用示例：
    ```python
    from load_tester import LoadTestEngine, LoadTestConfig, Scenario, ...

    # 定义场景
    scenario = Scenario(name="API测试")
    scenario.add_step(ScenarioStep(...))

    # 配置压测
    config = LoadTestConfig(
        scenario=scenario,
        load_mode="constant",
        duration=120,
        concurrency=50,
        qps=500,
    )

    # 运行
    engine = LoadTestEngine(config)
    result = engine.run()
    ```
    """

    def __init__(
        self,
        config: LoadTestConfig,
        on_phase_change: Optional[Callable[[str, dict], None]] = None,
        on_progress: Optional[Callable[[float, dict], None]] = None,
        custom_http_executor: Optional[Callable] = None,
    ):
        self._config = config
        self._on_phase_change = on_phase_change
        self._on_progress = on_progress
        self._custom_http_executor = custom_http_executor

        # 组件
        self._load_model: Optional[LoadModel] = None
        self._rate_limiter: Optional[RateLimiter] = None
        self._worker_pool: Optional[WorkerPool] = None
        self._collector: Optional[MetricsCollector] = None
        self._aggregator: Optional[MetricsAggregator] = None

        # 状态
        self._stop_event = threading.Event()
        self._progress_thread: Optional[threading.Thread] = None
        self._start_time = 0.0
        self._end_time = 0.0
        self._stopped_early = False

    def run(self) -> LoadTestResult:
        """执行压测

        Returns:
            LoadTestResult 包含最终指标和报告路径
        """
        try:
            self._initialize()
            self._start()
            self._wait_completion()
        except KeyboardInterrupt:
            print("\n\n[!] 收到中断信号，正在优雅停止...")
            self._stopped_early = True
        finally:
            self._stop_everything()

        return self._finalize()

    def stop(self) -> None:
        """外部调用：停止压测"""
        self._stop_event.set()
        self._stopped_early = True

    # ============ 内部方法 ============

    def _initialize(self) -> None:
        """初始化所有组件"""
        config = self._config

        # 1. 负载模型
        self._load_model = config.build_load_model()
        total_duration = self._load_model.get_total_duration()

        # 2. 速率限制器（如果配置了QPS）
        initial_qps = self._get_initial_qps(config)
        # 权重分摊模式：只要配置了 qps，就通过步骤级限速器按 weight 分摊总 QPS
        # 这样总 HTTP QPS = sum(step_qps) ≈ qps，和全局限速效果一致，但更灵活
        # 禁用全局 rate limiter，避免双重限制
        use_global_rate_limiter = not (initial_qps and initial_qps > 0 and len(config.scenario.steps) > 0)

        if initial_qps and use_global_rate_limiter:
            self._rate_limiter = TokenBucketRateLimiter(
                rate_per_second=initial_qps,
                busy_wait=config.rate_limiter_busy_wait,
            )

        # 3. 聚合器
        self._aggregator = MetricsAggregator(
            histogram_sig_digits=config.histogram_sig_digits,
        )

        # 4. 采集器
        self._collector = MetricsCollector(
            max_queue_size=config.collector_queue_size,
        )
        self._collector.add_sink("aggregator", StatsSinkAdapter(self._aggregator))

        # 5. 工作池
        self._worker_pool = WorkerPool(
            scenario=config.scenario,
            num_workers=self._get_initial_concurrency(config),
            http_executor=self._custom_http_executor,
            result_callback=self._collector.record_from_worker_result
            if self._collector else None,
            rate_limiter=self._rate_limiter,
            global_qps=self._get_initial_qps(config),
        )

    def _get_initial_qps(self, config: LoadTestConfig) -> Optional[float]:
        mode = config.load_mode.lower()
        if mode == "constant":
            return config.qps
        elif mode == "step":
            return config.steps[0][2] if config.steps else None
        elif mode == "ramp":
            return config.start_qps
        elif mode == "spike":
            return config.base_qps
        return None

    def _get_initial_concurrency(self, config: LoadTestConfig) -> int:
        mode = config.load_mode.lower()
        if mode == "constant":
            return config.concurrency
        elif mode == "step":
            return config.steps[0][1] if config.steps else 1
        elif mode == "ramp":
            return config.start_concurrency
        elif mode == "spike":
            return config.base_concurrency
        return 1

    def _start(self) -> None:
        """启动所有组件"""
        self._start_time = time.time()

        # 启动采集器（先于工作池，避免丢失样本）
        if self._collector:
            self._collector.start()

        # 启动工作池
        if self._worker_pool:
            self._worker_pool.start()

        # 应用负载模型
        if self._load_model and self._worker_pool:
            self._load_model.apply(
                worker_pool=self._worker_pool,
                rate_limiter=self._rate_limiter,
                on_phase_change=self._handle_phase_change,
                on_progress=self._handle_progress,
            )

        # 启动进度显示线程
        if self._config.enable_progress_bar and self._config.output_console:
            self._start_progress_thread()

    def _wait_completion(self) -> None:
        """等待压测自然完成"""
        total_duration = self._load_model.get_total_duration() if self._load_model else 0
        deadline = self._start_time + total_duration + 5

        while not self._stop_event.is_set():
            # 负载模型已停止运行
            if self._load_model and not self._load_model.is_running:
                break
            # 超时保护
            if time.time() > deadline:
                break
            # 工作池全部空闲也不可能，所以靠负载模型控制
            time.sleep(0.1)

    def _stop_everything(self) -> None:
        """停止所有组件"""
        self._stop_event.set()
        self._end_time = time.time()

        # 停止负载模型
        if self._load_model:
            self._load_model.stop()

        # 停止工作池
        if self._worker_pool:
            self._worker_pool.stop(timeout_per_worker=5.0)

        # 停止速率限制器
        if self._rate_limiter:
            self._rate_limiter.stop()

        # 等待进度线程
        if self._progress_thread and self._progress_thread.is_alive():
            self._progress_thread.join(timeout=2.0)

        # 停止采集器（最后停止，确保处理完所有样本）
        if self._collector:
            self._collector.stop(flush=True, timeout=3.0)

    def _finalize(self) -> LoadTestResult:
        """构建最终结果和报告"""
        metrics = self._aggregator.build() if self._aggregator else AggregatedMetrics()
        report_paths: Dict[str, str] = {}

        config = self._config
        report_dir = Path(config.report_dir or "./reports")
        report_dir.mkdir(parents=True, exist_ok=True)
        base_path = report_dir / config.report_name

        title = f"Load Test - {config.scenario.name}"

        # 控制台报告
        if config.output_console:
            reporter = ConsoleReporter()
            if self._config.enable_progress_bar:
                sys.stdout.write("\n\n")
            reporter.report(metrics, title=title)

        # JSON报告
        if config.output_json:
            json_path = base_path.with_suffix(".json")
            JsonReporter().report(metrics, json_path, title=title)
            report_paths["json"] = str(json_path)

        # HTML报告
        if config.output_html:
            html_path = base_path.with_suffix(".html")
            HtmlReporter().report(metrics, html_path, title=title)
            report_paths["html"] = str(html_path)

        if report_paths:
            print(f"\n📁 报告文件已生成:")
            for kind, path in report_paths.items():
                print(f"   [{kind.upper()}] {path}")

        # 收集参数使用统计
        parameter_stats = []
        csv_stats = []
        if self._worker_pool is not None:
            try:
                parameter_stats = self._worker_pool.get_parameter_stats()
                csv_stats = self._worker_pool.get_csv_stats_summary()
            except Exception:
                pass
        metrics.parameter_stats = parameter_stats
        metrics.csv_stats = csv_stats

        result = LoadTestResult(
            config=config,
            metrics=metrics,
            report_paths=report_paths,
            start_time=self._start_time,
            end_time=self._end_time,
            duration=self._end_time - self._start_time,
            stopped_early=self._stopped_early,
            parameter_stats=parameter_stats,
            csv_stats=csv_stats,
        )
        return result

    def _start_progress_thread(self) -> None:
        """启动实时进度显示线程"""
        console = ConsoleReporter(show_progress=False)
        last_progress = 0.0
        total_duration = self._load_model.get_total_duration() if self._load_model else 0

        def _progress_loop():
            nonlocal last_progress
            last_state = {"phase": "init", "active_workers": 0}
            while not self._stop_event.is_set():
                elapsed = time.time() - self._start_time
                progress = min(100.0, (elapsed / total_duration) * 100) if total_duration else 0

                realtime = (
                    self._aggregator.get_realtime_stats(window_seconds=10.0)
                    if self._aggregator else {}
                )

                # 补全缺少的字段
                state = dict(last_state)
                state["active_workers"] = (
                    self._worker_pool.active_workers if self._worker_pool else 0
                )

                # 使用全局进度
                console.report_progress(progress, state, realtime)
                last_progress = progress

                time.sleep(self._config.progress_interval)

        self._progress_thread = threading.Thread(
            target=_progress_loop, name="ProgressReporter", daemon=True,
        )
        self._progress_thread.start()

    def _handle_phase_change(self, phase_name: str, phase_info: dict) -> None:
        """阶段变化回调"""
        if self._on_phase_change:
            try:
                self._on_phase_change(phase_name, phase_info)
            except Exception:
                pass

    def _handle_progress(self, progress_pct: float, state: dict) -> None:
        """进度回调"""
        if self._on_progress:
            try:
                self._on_progress(progress_pct, state)
            except Exception:
                pass
